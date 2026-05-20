import json
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from app import (
    COLOR_PALETTES,
    apply_indicator_to_geojson,
    load_pernambuco_municipios_geojson,
    normalize_municipio_name,
    read_indicator_upload,
    render_svg_pernambuco_map,
)


def display_sidebar_logo():
    for logo in (Path('combinado.png'), Path('assets/combinado.png')):
        if logo.exists():
            cols = st.sidebar.columns([0.5, 3, 0.5])
            cols[1].image(str(logo), use_container_width=True)
            st.sidebar.markdown('---')
            return


def render_map_app():
    st.set_page_config(page_title='Mapa de Pernambuco', page_icon='🗺️', layout='wide')
    display_sidebar_logo()

    st.title('Mapa de Pernambuco por Município')
    st.markdown(
        'Este aplicativo gera um mapa municipal de Pernambuco com os nomes dos municípios fixos sobre '
        'cada território. Você pode carregar uma planilha CSV ou Excel com indicadores por município '
        'ou código IBGE, escolher a coluna do indicador, definir cores por escala automática ou faixas '
        'personalizadas, exibir valores no mapa e baixar o resultado em SVG para uso em relatório.'
    )

    try:
        base_geojson = load_pernambuco_municipios_geojson()
    except Exception as exc:
        st.error('Não foi possível carregar a malha municipal do IBGE.')
        st.exception(exc)
        return

    st.sidebar.markdown('### Mapa')
    label_size = st.sidebar.slider('Tamanho dos nomes', min_value=5, max_value=16, value=6)
    label_color = st.sidebar.color_picker('Cor dos nomes', '#111827')
    boundary_color = st.sidebar.color_picker('Cor das divisas municipais', '#ffffff')
    label_background = st.sidebar.checkbox('Reforçar contorno branco dos nomes', value=True)
    show_values = st.sidebar.checkbox('Mostrar valor junto ao nome', value=False)

    value_format = 'Decimal'
    decimal_places = 2
    if show_values:
        value_format = st.sidebar.radio('Formato do valor', ['Inteiro', 'Decimal'], horizontal=True)
        if value_format == 'Decimal':
            decimal_places = st.sidebar.number_input('Casas decimais', min_value=1, max_value=4, value=2, step=1)

    uploaded_indicator = st.sidebar.file_uploader(
        'Indicadores CSV ou Excel',
        type=['csv', 'xlsx', 'xls'],
        help='Use uma coluna com Município ou código IBGE e outra coluna numérica para o indicador.'
    )

    geojson = base_geojson
    matched = None
    min_value = None
    max_value = None
    indicator_df = None
    color_mode = 'Escala automatica'
    min_color = '#deebf7'
    max_color = '#08519c'
    color_rules = []
    palette_name = 'Azul'
    class_count = 7
    zero_color_enabled = False
    zero_color = '#f3f4f6'

    if uploaded_indicator:
        try:
            indicator_df = read_indicator_upload(uploaded_indicator)
        except Exception as exc:
            st.sidebar.error('Não consegui ler o arquivo de indicadores.')
            st.sidebar.exception(exc)

    if indicator_df is not None and not indicator_df.empty:
        columns = list(indicator_df.columns)
        normalized_columns = {col: normalize_municipio_name(col) for col in columns}
        municipio_guess = next(
            (col for col, normalized in normalized_columns.items()
             if normalized in {'MUNICIPIO', 'CIDADE', 'NOME', 'CODIGO IBGE', 'IBGE', 'COD IBGE'}),
            columns[0]
        )
        numeric_columns = [
            col for col in columns
            if pd.to_numeric(indicator_df[col], errors='coerce').notna().any()
        ]
        value_guess = numeric_columns[0] if numeric_columns else columns[-1]
        municipio_col = st.sidebar.selectbox('Coluna do Município/código', columns, index=columns.index(municipio_guess))
        value_col = st.sidebar.selectbox('Coluna do indicador', columns, index=columns.index(value_guess))

        st.sidebar.markdown('### Cores do indicador')
        color_mode = st.sidebar.radio(
            'Regra de coloração',
            ['Escala automática', 'Faixas personalizadas'],
            horizontal=False
        )

        if color_mode == 'Escala automática':
            palette_name = st.sidebar.selectbox('Paleta', list(COLOR_PALETTES.keys()) + ['Personalizada'])
            class_count = st.sidebar.slider('Quantidade de gradações', min_value=3, max_value=12, value=7)
            zero_color_enabled = st.sidebar.checkbox('Usar cor especifica para valor zero', value=False)
            if zero_color_enabled:
                zero_color = st.sidebar.color_picker('Cor do zero', '#f3f4f6')
            if palette_name == 'Personalizada':
                min_color = st.sidebar.color_picker('Cor do menor valor', '#deebf7')
                max_color = st.sidebar.color_picker('Cor do maior valor', '#08519c')
        else:
            rule_count = st.sidebar.number_input('Quantidade de faixas', min_value=1, max_value=8, value=3, step=1)
            default_colors = ['#de2d26', '#ffeda0', '#2ca25f', '#756bb1', '#3182bd', '#636363', '#f768a1', '#feb24c']
            default_modes = ['Igual a', 'Intervalo', 'Maior que']
            st.sidebar.caption('Exemplo: = 0; >= 1 e < 40; >= 40.')
            for index in range(int(rule_count)):
                with st.sidebar.expander(f'Faixa {index + 1}', expanded=index < 3):
                    mode = st.selectbox(
                        'Tipo de faixa',
                        ['Igual a', 'Menor que', 'Maior que', 'Intervalo'],
                        index=['Igual a', 'Menor que', 'Maior que', 'Intervalo'].index(
                            default_modes[index] if index < len(default_modes) else 'Intervalo'
                        ),
                        key=f'color_rule_operator_{index}'
                    )
                    min_operator = '>='
                    max_operator = '<='
                    min_text = ''
                    max_text = ''

                    if mode == 'Igual a':
                        value_text = st.text_input('Valor', value='0' if index == 0 else '', key=f'color_rule_value_{index}')
                        operator = '='
                        min_text = value_text
                    elif mode == 'Menor que':
                        max_operator = st.selectbox('Comparacao', ['<', '<='], key=f'color_rule_max_operator_{index}')
                        max_text = st.text_input('Valor', value='0' if index == 0 else '', key=f'color_rule_max_{index}')
                        operator = 'Intervalo'
                    elif mode == 'Maior que':
                        min_operator = st.selectbox('Comparacao', ['>=', '>'], key=f'color_rule_min_operator_{index}')
                        min_text = st.text_input('Valor', value='40' if index == 2 else '', key=f'color_rule_min_{index}')
                        operator = 'Intervalo'
                    else:
                        col_a, col_b = st.columns(2)
                        with col_a:
                            min_operator = st.selectbox('Limite inferior', ['>=', '>'], key=f'color_rule_min_operator_{index}')
                            min_text = st.text_input('De', value='1' if index == 1 else '', key=f'color_rule_min_{index}')
                        with col_b:
                            max_operator = st.selectbox('Limite superior', ['<', '<='], key=f'color_rule_max_operator_{index}')
                            max_text = st.text_input('Até', value='40' if index == 1 else '', key=f'color_rule_max_{index}')
                        operator = 'Intervalo'

                    color = st.color_picker('Cor', default_colors[index], key=f'color_rule_color_{index}')
                    min_rule = pd.to_numeric(min_text.replace(',', '.') if min_text else None, errors='coerce')
                    max_rule = pd.to_numeric(max_text.replace(',', '.') if max_text else None, errors='coerce')
                    color_rules.append({
                        'operator': operator,
                        'min_operator': min_operator,
                        'max_operator': max_operator,
                        'min': None if pd.isna(min_rule) else float(min_rule),
                        'max': None if pd.isna(max_rule) else float(max_rule),
                        'color': color
                    })

        geojson, matched, min_value, max_value = apply_indicator_to_geojson(
            base_geojson,
            indicator_df,
            municipio_col,
            value_col,
            color_mode=color_mode,
            min_color=min_color,
            max_color=max_color,
            palette_name=palette_name,
            class_count=class_count,
            zero_color_enabled=zero_color_enabled,
            zero_color=zero_color,
            color_rules=color_rules
        )

    if not show_values:
        geojson = json.loads(json.dumps(geojson))
        for feature in geojson.get('features', []):
            feature.setdefault('properties', {})['indicador'] = None

    svg_map = render_svg_pernambuco_map(
        geojson,
        label_size=label_size,
        label_color=label_color,
        label_background=label_background,
        show_values=show_values,
        value_format=value_format,
        decimal_places=decimal_places,
        boundary_color=boundary_color
    )

    st.download_button(
        'Baixar mapa em SVG para o relatório',
        data=svg_map.encode('utf-8'),
        file_name='mapa_pernambuco_municipios.svg',
        mime='image/svg+xml'
    )
    components.html(svg_map, height=980, scrolling=True)

    if matched is not None:
        col1, col2, col3 = st.columns(3)
        col1.metric('Municípios encontrados', matched)
        col2.metric('Menor valor', '-' if min_value is None else f'{min_value:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.'))
        col3.metric('Maior valor', '-' if max_value is None else f'{max_value:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.'))

    template = pd.DataFrame({
        'codigo_ibge': [feature.get('properties', {}).get('codigo_ibge') for feature in base_geojson.get('features', [])],
        'municipio': [feature.get('properties', {}).get('municipio') for feature in base_geojson.get('features', [])],
        'indicador': ['' for _ in base_geojson.get('features', [])]
    }).sort_values('municipio')
    st.download_button(
        'Baixar modelo de planilha de indicadores',
        data=template.to_csv(index=False, sep=';').encode('utf-8-sig'),
        file_name='modelo_indicadores_municipios_pe.csv',
        mime='text/csv'
    )


if __name__ == '__main__':
    render_map_app()
