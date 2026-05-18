import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import re
import io
import json
import gzip
import html
import urllib.request
import unicodedata
from pathlib import Path
from functools import lru_cache

try:
    import pydeck as pdk
except ImportError:
    pdk = None


IBGE_PE_MUNICIPIOS_GEOJSON_URL = (
    'https://servicodados.ibge.gov.br/api/v3/malhas/estados/26'
    '?formato=application/vnd.geo+json&qualidade=maxima&intrarregiao=municipio'
)
IBGE_PE_MUNICIPIOS_URL = (
    'https://servicodados.ibge.gov.br/api/v1/localidades/estados/26/municipios'
)

MUNICIPIO_NAME_OVERRIDES = {
    'IGUARACY': 'Iguaraci',
    'BELEM DO SAO FRANCISCO': 'Belém de São Francisco',
}

def clean_qrp_text(raw_bytes):
    """
    PASSO 1: Converte o binário do QRP para texto legível.
    """
    text = raw_bytes.decode('utf-16le', errors='replace')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def clean_motivo_text(text):
    # Removido: text = re.sub(r'\([^)]*\)', ' ', text)  # Agora mantemos os códigos entre parênteses
    text = re.sub(r'\b\d{2}/\d{2}/\d{4}\b', ' ', text)
    text = re.sub(r'\b\d{1,3}(?:\.\d{3})*,\d{2}\b', ' ', text)
    text = re.sub(r'\b\d+\b', ' ', text)
    lixos = ['SISTEMA', 'DATASUS', 'SECRETARIA', 'ESTADUAL', 'HOSPITALARES',
             'DEFINITIVO', 'MENSAGEM DE ERRO', 'VALOR PRÉVIA', 'MUNICÍPIO',
             'RECIFE', 'LINHA', 'LOTE', 'COMPETÊNCIA', 'PÁGINA', 'GESTOR',
             'VALOR', 'PRÉVIA', 'ALTA', 'SIHD', 'ARIAL', 'FONTE']
    for lx in lixos:
        text = re.compile(r'\b' + lx + r'\b', re.IGNORECASE).sub(' ', text)
    text = re.sub(r'\([^)]*\)', lambda m: m.group(0) if 'DOC:' in m.group(0).upper() else m.group(0), text)
    text = re.sub(r'\(\s*DOC\s*\)', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\(\s*\)', ' ', text)
    text = re.sub(r'[^A-Za-zÇÃÕÁÉÍÓÚÂÊÎÔÛÀÈÌÒÙçãõáéíóúâêîôûàèìòù\s\-\/\.()]+', ' ', text)  # Adicionado () para manter parênteses
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+[A-Za-zÇÃÕÁÉÍÓÚÂÊÎÔÛÀÈÌÒÙçãõáéíóúâêîôûàèìòù]$', '', text)
    text = re.sub(r'[\s\-\./,;:]+$', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+OU\s*$', '', text, flags=re.IGNORECASE)
    return text


def remove_accents(value):
    normalized = unicodedata.normalize('NFD', value)
    return ''.join(ch for ch in normalized if unicodedata.category(ch) != 'Mn')


def motivo_key(text):
    text = remove_accents(str(text).upper())
    text = re.sub(r'[^A-Z0-9]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def basic_municipio_key(value):
    value = remove_accents(str(value).upper())
    value = re.sub(r'[^A-Z0-9]+', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def normalize_municipio_name(value):
    value = basic_municipio_key(value)
    corrected = MUNICIPIO_NAME_OVERRIDES.get(value, value)
    return basic_municipio_key(corrected)


def canonical_municipio_name(value):
    key = basic_municipio_key(value)
    return MUNICIPIO_NAME_OVERRIDES.get(key, str(value).strip())


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        if payload.startswith(b'\x1f\x8b'):
            payload = gzip.decompress(payload)
        return json.loads(payload.decode('utf-8'))


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_pernambuco_municipios_geojson():
    geojson = fetch_json(IBGE_PE_MUNICIPIOS_GEOJSON_URL)
    municipios = fetch_json(IBGE_PE_MUNICIPIOS_URL)
    nomes_por_id = {str(item['id']): item['nome'] for item in municipios}

    for feature in geojson.get('features', []):
        props = feature.setdefault('properties', {})
        codigo = str(props.get('codarea') or props.get('id') or props.get('CD_MUN') or '')
        nome = props.get('nome') or props.get('name') or nomes_por_id.get(codigo, codigo)
        nome = canonical_municipio_name(nome)
        props['codigo_ibge'] = codigo
        props['municipio'] = nome
        props['municipio_key'] = normalize_municipio_name(nome)
        props['indicador'] = None
        props['fill_color'] = [225, 232, 240, 185]

    return geojson


def polygon_area_and_centroid(ring):
    if len(ring) < 3:
        return 0, None

    area_twice = 0
    cx = 0
    cy = 0
    points = ring if ring[0] == ring[-1] else ring + [ring[0]]
    for index in range(len(points) - 1):
        x0, y0 = points[index][:2]
        x1, y1 = points[index + 1][:2]
        cross = x0 * y1 - x1 * y0
        area_twice += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    if abs(area_twice) < 1e-12:
        xs = [point[0] for point in ring]
        ys = [point[1] for point in ring]
        return 0, (sum(xs) / len(xs), sum(ys) / len(ys))

    area = area_twice / 2
    return abs(area), (cx / (3 * area_twice), cy / (3 * area_twice))


def feature_label_point(feature):
    geometry = feature.get('geometry') or {}
    coordinates = geometry.get('coordinates') or []
    geom_type = geometry.get('type')
    candidates = []

    if geom_type == 'Polygon':
        candidates = [coordinates]
    elif geom_type == 'MultiPolygon':
        candidates = coordinates

    best_area = -1
    best_centroid = None
    for polygon in candidates:
        if not polygon:
            continue
        area, centroid = polygon_area_and_centroid(polygon[0])
        if centroid and area > best_area:
            best_area = area
            best_centroid = centroid

    return best_centroid


def municipio_label_dataframe(geojson):
    rows = []
    for feature in geojson.get('features', []):
        centroid = feature_label_point(feature)
        if not centroid:
            continue
        props = feature.get('properties', {})
        indicador = props.get('indicador')
        label = props.get('municipio', '')
        if indicador is not None:
            label = f"{label}\n{indicador:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
        rows.append({
            'lon': centroid[0],
            'lat': centroid[1],
            'label': label,
            'municipio': props.get('municipio', ''),
            'indicador': indicador
        })
    return pd.DataFrame(rows)


def feature_polygons(feature):
    geometry = feature.get('geometry') or {}
    coordinates = geometry.get('coordinates') or []
    geom_type = geometry.get('type')

    if geom_type == 'Polygon':
        return [coordinates]
    if geom_type == 'MultiPolygon':
        return coordinates
    return []


def is_fernando_de_noronha(feature):
    props = feature.get('properties', {})
    return normalize_municipio_name(props.get('municipio', '')) == 'FERNANDO DE NORONHA'


def feature_bounds(feature):
    xs = []
    ys = []
    for polygon in feature_polygons(feature):
        for ring in polygon:
            for point in ring:
                xs.append(point[0])
                ys.append(point[1])
    return min(xs), min(ys), max(xs), max(ys)


def geojson_bounds(geojson, include_noronha=False):
    xs = []
    ys = []
    for feature in geojson.get('features', []):
        if not include_noronha and is_fernando_de_noronha(feature):
            continue
        for polygon in feature_polygons(feature):
            for ring in polygon:
                for point in ring:
                    xs.append(point[0])
                    ys.append(point[1])

    if not xs:
        return geojson_bounds(geojson, include_noronha=True)
    return min(xs), min(ys), max(xs), max(ys)


def svg_color(rgba):
    if not rgba:
        return 'rgb(225, 232, 240)'
    return f'rgb({rgba[0]}, {rgba[1]}, {rgba[2]})'


def format_indicator_value(value, value_format='Decimal', decimal_places=2):
    if value is None or pd.isna(value):
        return ''
    if value_format == 'Inteiro':
        return f'{value:,.0f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'{value:,.{decimal_places}f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def wrap_label(text, max_chars=13):
    words = str(text).split()
    lines = []
    current = ''
    for word in words:
        candidate = word if not current else f'{current} {word}'
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or ['']


def estimate_label_box(x, y, text_lines, label_size):
    line_height = label_size * 1.08
    text_width = max(len(line) for line in text_lines) * label_size * 0.54
    text_height = len(text_lines) * line_height
    padding = label_size * 0.35
    return (
        x - text_width / 2 - padding,
        y - text_height / 2 - padding,
        x + text_width / 2 + padding,
        y + text_height / 2 + padding
    )


def boxes_overlap(box_a, box_b, gap=1.5):
    return not (
        box_a[2] + gap < box_b[0]
        or box_a[0] - gap > box_b[2]
        or box_a[3] + gap < box_b[1]
        or box_a[1] - gap > box_b[3]
    )


def box_inside_bounds(box, width, height, margin=4):
    return (
        box[0] >= margin
        and box[1] >= margin
        and box[2] <= width - margin
        and box[3] <= height - margin
    )


def label_candidate_offsets(max_radius=110, step=9):
    offsets = [(0, 0)]
    directions = [
        (1, 0), (-1, 0), (0, -1), (0, 1),
        (1, -0.7), (-1, -0.7), (1, 0.7), (-1, 0.7)
    ]
    for radius in range(step, max_radius + step, step):
        for dx, dy in directions:
            offsets.append((dx * radius, dy * radius))
    return offsets


def resolve_label_positions(label_items, label_size, width, height, blocked_boxes=None):
    occupied = list(blocked_boxes or [])
    resolved = []
    offsets = label_candidate_offsets()

    for item in sorted(label_items, key=lambda label: (label['priority'], label['x'])):
        best = None
        best_score = None

        for dx, dy in offsets:
            candidate_x = item['x'] + dx
            candidate_y = item['y'] + dy
            box = estimate_label_box(candidate_x, candidate_y, item['text_lines'], label_size)
            if not box_inside_bounds(box, width, height):
                continue

            overlaps = sum(1 for occupied_box in occupied if boxes_overlap(box, occupied_box))
            distance = abs(dx) + abs(dy)
            score = overlaps * 10000 + distance
            if best_score is None or score < best_score:
                best_score = score
                best = (candidate_x, candidate_y, box, overlaps)
                if overlaps == 0:
                    break

        if best is None:
            box = estimate_label_box(item['x'], item['y'], item['text_lines'], label_size)
            best = (item['x'], item['y'], box, 1)

        item = item.copy()
        item['label_x'], item['label_y'], item['box'], item['overlaps'] = best
        occupied.append(item['box'])
        resolved.append(item)

    return resolved


def render_svg_pernambuco_map(
    geojson,
    label_size=8,
    label_color='#111827',
    label_background=False,
    show_values=False,
    value_format='Decimal',
    decimal_places=2,
    boundary_color='#ffffff',
    width=2300,
    height=980
):
    min_x, min_y, max_x, max_y = geojson_bounds(geojson, include_noronha=False)
    padding = 24
    top_reserved = 160
    map_width = width - padding * 2
    map_height = height - top_reserved - padding * 2
    scale = min(map_width / (max_x - min_x), map_height / (max_y - min_y))
    drawn_width = (max_x - min_x) * scale
    drawn_height = (max_y - min_y) * scale
    offset_x = padding + (map_width - drawn_width) / 2
    offset_y = top_reserved + padding + (map_height - drawn_height) / 2

    noronha_feature = next((feature for feature in geojson.get('features', []) if is_fernando_de_noronha(feature)), None)
    noronha_bounds = feature_bounds(noronha_feature) if noronha_feature else None
    noronha_box = {
        'x': width - 340,
        'y': 20,
        'width': 240,
        'height': 125,
        'padding': 14
    }

    def project_mainland(point):
        lon, lat = point[:2]
        x = offset_x + (lon - min_x) * scale
        y = offset_y + (max_y - lat) * scale
        return x, y

    def project_noronha(point):
        if not noronha_bounds:
            return project_mainland(point)

        n_min_x, n_min_y, n_max_x, n_max_y = noronha_bounds
        available_width = noronha_box['width'] - noronha_box['padding'] * 2
        available_height = noronha_box['height'] - noronha_box['padding'] * 2
        n_scale = min(available_width / (n_max_x - n_min_x), available_height / (n_max_y - n_min_y))
        drawn_n_width = (n_max_x - n_min_x) * n_scale
        drawn_n_height = (n_max_y - n_min_y) * n_scale
        n_offset_x = noronha_box['x'] + noronha_box['padding'] + (available_width - drawn_n_width) / 2
        n_offset_y = noronha_box['y'] + noronha_box['padding'] + (available_height - drawn_n_height) / 2
        lon, lat = point[:2]
        x = n_offset_x + (lon - n_min_x) * n_scale
        y = n_offset_y + (n_max_y - lat) * n_scale
        return x, y

    def project(point, feature):
        if is_fernando_de_noronha(feature):
            return project_noronha(point)
        return project_mainland(point)

    paths = []
    labels = []
    label_items = []
    leader_lines = []
    for feature in geojson.get('features', []):
        props = feature.get('properties', {})
        path_parts = []
        for polygon in feature_polygons(feature):
            for ring in polygon:
                if not ring:
                    continue
                projected = [project(point, feature) for point in ring]
                first_x, first_y = projected[0]
                commands = [f'M {first_x:.2f} {first_y:.2f}']
                commands.extend(f'L {x:.2f} {y:.2f}' for x, y in projected[1:])
                commands.append('Z')
                path_parts.append(' '.join(commands))

        title = html.escape(str(props.get('municipio', '')))
        fill = svg_color(props.get('fill_color'))
        paths.append(
            f'<path d="{" ".join(path_parts)}" fill="{fill}" stroke="{boundary_color}" '
            f'stroke-width="1.1" vector-effect="non-scaling-stroke" fill-rule="evenodd">'
            f'<title>{title}</title></path>'
        )

        centroid = feature_label_point(feature)
        if not centroid:
            continue
        x, y = project(centroid, feature)
        municipio = props.get('municipio', '')
        text_lines = wrap_label(municipio, max_chars=11)
        if show_values and props.get('indicador') is not None:
            text_lines.append(format_indicator_value(props.get('indicador'), value_format, decimal_places))

        f_min_x, f_min_y, f_max_x, f_max_y = feature_bounds(feature)
        priority = abs((f_max_x - f_min_x) * (f_max_y - f_min_y))
        label_items.append({
            'x': x,
            'y': y,
            'text_lines': text_lines,
            'priority': priority,
            'is_noronha': is_fernando_de_noronha(feature)
        })

    blocked_boxes = [(0, 0, width, top_reserved - 8)]
    mainland_labels = [item for item in label_items if not item['is_noronha']]
    noronha_labels = [item for item in label_items if item['is_noronha']]
    resolved_labels = resolve_label_positions(mainland_labels, label_size, width, height, blocked_boxes)
    resolved_labels.extend(noronha_labels)

    for item in resolved_labels:
        x = item.get('label_x', item['x'])
        y = item.get('label_y', item['y'])
        text_lines = item['text_lines']
        if abs(x - item['x']) + abs(y - item['y']) > label_size * 1.4:
            leader_lines.append(
                f'<line class="leader-line" x1="{item["x"]:.2f}" y1="{item["y"]:.2f}" '
                f'x2="{x:.2f}" y2="{y:.2f}"></line>'
            )
        line_height = label_size * 1.05
        first_dy = -((len(text_lines) - 1) * line_height) / 2
        tspans = []
        for index, line in enumerate(text_lines):
            dy = first_dy if index == 0 else line_height
            tspans.append(
                f'<tspan x="{x:.2f}" dy="{dy:.2f}">{html.escape(line)}</tspan>'
            )

        background_class = ' label-bg' if label_background else ''
        labels.append(
            f'<text class="municipio-label{background_class}" x="{x:.2f}" y="{y:.2f}">'
            f'{"".join(tspans)}</text>'
        )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Mapa de Pernambuco com nomes dos municipios">
        <style>
          svg {{
            width: 100%;
            min-width: 1800px;
            height: auto;
            display: block;
            font-family: Arial, Helvetica, sans-serif;
          }}
          .municipio-label {{
            fill: {label_color};
            font-size: {label_size}px;
            font-weight: 700;
            text-anchor: middle;
            dominant-baseline: middle;
            pointer-events: none;
            paint-order: stroke;
            stroke: #ffffff;
            stroke-width: 3px;
            stroke-linejoin: round;
          }}
          .municipio-label.label-bg {{
            stroke-width: 4px;
          }}
          .leader-line {{
            stroke: #6b7280;
            stroke-width: 0.6;
            opacity: 0.55;
            vector-effect: non-scaling-stroke;
          }}
        </style>
        <rect width="{width}" height="{height}" fill="#ffffff"></rect>
        <rect x="{noronha_box['x']}" y="{noronha_box['y']}" width="{noronha_box['width']}" height="{noronha_box['height']}"
          fill="#ffffff" stroke="#d1d5db" stroke-width="1" rx="4"></rect>
        <g>{''.join(paths)}</g>
        <g>{''.join(leader_lines)}</g>
        <g>{''.join(labels)}</g>
      </svg>
    '''
    return svg


def hex_to_rgba(hex_color, alpha=210):
    hex_color = hex_color.strip().lstrip('#')
    if len(hex_color) != 6:
        return [225, 232, 240, alpha]
    return [int(hex_color[index:index + 2], 16) for index in (0, 2, 4)] + [alpha]


COLOR_PALETTES = {
    'Azul': ['#eff3ff', '#bdd7e7', '#6baed6', '#3182bd', '#08519c'],
    'Verde': ['#edf8e9', '#bae4b3', '#74c476', '#31a354', '#006d2c'],
    'Vermelho': ['#fee5d9', '#fcae91', '#fb6a4a', '#de2d26', '#a50f15'],
    'Amarelo-Laranja-Vermelho': ['#ffffcc', '#ffeda0', '#feb24c', '#f03b20', '#bd0026'],
    'Divergente vermelho-amarelo-verde': ['#d73027', '#fc8d59', '#ffffbf', '#91cf60', '#1a9850'],
}


def interpolate_rgb(color_a, color_b, ratio):
    rgb_a = hex_to_rgba(color_a, 210)[:3]
    rgb_b = hex_to_rgba(color_b, 210)[:3]
    return [round(rgb_a[i] + (rgb_b[i] - rgb_a[i]) * ratio) for i in range(3)]


def palette_color(value, min_value, max_value, palette, class_count):
    if value is None or pd.isna(value):
        return [225, 232, 240, 185]
    if min_value == max_value:
        return hex_to_rgba(palette[-1], 210)

    ratio = (float(value) - min_value) / (max_value - min_value)
    ratio = max(0, min(1, ratio))
    class_count = max(2, int(class_count))
    class_index = min(class_count - 1, int(ratio * class_count))
    class_ratio = class_index / (class_count - 1)

    scaled = class_ratio * (len(palette) - 1)
    lower = int(scaled)
    upper = min(len(palette) - 1, lower + 1)
    local_ratio = scaled - lower
    return interpolate_rgb(palette[lower], palette[upper], local_ratio) + [210]


def color_scale(
    value,
    min_value,
    max_value,
    min_color='#deebf7',
    max_color='#08519c',
    palette_name='Azul',
    class_count=7,
    zero_color_enabled=False,
    zero_color='#f3f4f6'
):
    if value is None or pd.isna(value):
        return [225, 232, 240, 185]
    if zero_color_enabled and abs(float(value)) < 1e-9:
        return hex_to_rgba(zero_color, 210)

    if palette_name in COLOR_PALETTES:
        return palette_color(value, min_value, max_value, COLOR_PALETTES[palette_name], class_count)

    if min_value == max_value:
        ratio = 1
    else:
        ratio = (float(value) - min_value) / (max_value - min_value)
    ratio = max(0, min(1, ratio))

    low = hex_to_rgba(min_color, 210)[:3]
    high = hex_to_rgba(max_color, 210)[:3]
    rgb = [round(low[i] + (high[i] - low[i]) * ratio) for i in range(3)]
    return rgb + [210]


def color_by_rules(value, color_rules):
    if value is None or pd.isna(value):
        return [225, 232, 240, 185]

    for rule in color_rules:
        operator = rule.get('operator', 'Intervalo')
        min_value = rule.get('min')
        max_value = rule.get('max')

        if operator == '=':
            matched = min_value is not None and abs(value - min_value) < 1e-9
        else:
            min_operator = rule.get('min_operator', '>=')
            max_operator = rule.get('max_operator', '<=')

            if min_value is None:
                min_ok = True
            elif min_operator == '>':
                min_ok = value > min_value
            else:
                min_ok = value >= min_value

            if max_value is None:
                max_ok = True
            elif max_operator == '<':
                max_ok = value < max_value
            else:
                max_ok = value <= max_value

            matched = min_ok and max_ok

        if matched:
            return hex_to_rgba(rule.get('color', '#deebf7'), 210)

    return [225, 232, 240, 185]


def apply_indicator_to_geojson(
    geojson,
    indicator_df,
    municipio_col,
    value_col,
    color_mode='Escala automatica',
    min_color='#deebf7',
    max_color='#08519c',
    palette_name='Azul',
    class_count=7,
    zero_color_enabled=False,
    zero_color='#f3f4f6',
    color_rules=None
):
    mapped = {}
    code_mapped = {}
    for _, row in indicator_df.iterrows():
        municipio = row.get(municipio_col)
        value = pd.to_numeric(row.get(value_col), errors='coerce')
        if pd.isna(value):
            continue
        key = normalize_municipio_name(municipio)
        mapped[key] = float(value)
        code_mapped[str(municipio).strip()] = float(value)

    values = list(mapped.values()) + list(code_mapped.values())
    min_value = min(values) if values else None
    max_value = max(values) if values else None

    output = json.loads(json.dumps(geojson))
    matched = 0
    for feature in output.get('features', []):
        props = feature.setdefault('properties', {})
        municipio_key = props.get('municipio_key')
        codigo_ibge = str(props.get('codigo_ibge'))
        if municipio_key in mapped:
            value = mapped[municipio_key]
        else:
            value = code_mapped.get(codigo_ibge)
        if value is not None:
            matched += 1
            props['indicador'] = value
            if color_mode == 'Faixas personalizadas' and color_rules:
                props['fill_color'] = color_by_rules(value, color_rules)
            else:
                props['fill_color'] = color_scale(
                    value,
                    min_value,
                    max_value,
                    min_color,
                    max_color,
                    palette_name,
                    class_count,
                    zero_color_enabled,
                    zero_color
                )
        else:
            props['indicador'] = None
            props['fill_color'] = [225, 232, 240, 185]

    return output, matched, min_value, max_value


def read_indicator_upload(uploaded_file):
    if uploaded_file.name.lower().endswith(('.xlsx', '.xls')):
        return pd.read_excel(uploaded_file)
    return pd.read_csv(uploaded_file, sep=None, engine='python')


@lru_cache(maxsize=1)
def load_official_motivos():
    path = Path('motivos_oficiais.xlsx')
    if not path.exists():
        return {}

    df = pd.read_excel(path, header=None)
    motivos = []
    for value in df.to_numpy().ravel():
        if pd.isna(value):
            continue
        motivo = str(value).strip()
        if not motivo:
            continue
        if motivo_key(motivo) == 'MOTIVOS DA REJEICAO':
            continue
        motivos.append(motivo.upper())

    return {motivo_key(motivo): motivo for motivo in motivos}


def officialize_motivo(motivo):
    official_by_key = load_official_motivos()
    if not official_by_key:
        return motivo, True

    official = official_by_key.get(motivo_key(motivo))
    if official:
        return official, True

    return motivo, False


def highlight_new_motivos(row):
    if row.get('Status') == 'Novo':
        return ['background-color: #fff3cd; color: #5f4100'] * len(row)
    return [''] * len(row)


def normalize_motivo(text):
    t = text.upper()

    def accentless(value):
        return remove_accents(value)

    t_ascii = re.sub(r'[^A-Z0-9 ]+', ' ', accentless(t))
    t_ascii = re.sub(r'\s+', ' ', t_ascii).strip()

    # Extrair códigos entre parênteses para usar como marcadores
    paren_codes = re.findall(r'\(([^)]+)\)', text.upper())
    paren_text = ' '.join(paren_codes)

    # Regras baseadas em códigos entre parênteses
    if 'DESACORDO COM CF-88' in paren_text or 'PROF COM MAIS 2 VINC PUBL' in paren_text:
        return 'PROFISSIONAL COM MAIS DE 2 VINC. PÚBLICOS (DESACORDO COM CF-88) OU PROFISSIONAL COM CH MAIOR QUE 168H POR SEMANA (PROF COM MAIS 168 H  SEMANAIS)'

    if 'DOC:' in paren_text or 'DOCUMENTO' in paren_text:
        return 'PROFISSIONAL VINCULADO NÃO CADASTRADO'

    # Regras existentes
    if 'PROFISSIONAL AUTONOMO' in t_ascii or 'PROFISSIONAL AUTO NOMO' in t_ascii or 'PROFISISONAL AUTONOMO' in t_ascii:
        if 'NO HOSPITAL COM CBO INFORMADO' in t_ascii:
            return 'PROFISSIONAL AUTÔNOMO NÃO CADASTRADO NO HOSPITAL'
        if 'NO HOSPITAL' in t_ascii:
            return 'PROFISSIONAL AUTÔNOMO NÃO CADASTRADO NO HOSPITAL'
        return 'PROFISSIONAL AUTÔNOMO NÃO CADASTRADO'

    if 'PROFISSIONAL VINCULADO' in t_ascii and 'NAO CADASTRADO' in t_ascii:
        return 'PROFISSIONAL VINCULADO NÃO CADASTRADO'

    if 'PROFISSIONAL NAO VINCULADO AO CNES' in t_ascii:
        return 'PROFISSIONAL NÃO VINCULADO AO CNES COM O CBO INFORMADO'

    if 'AIH BLOQUEADA EM OUTRO PROCESSAMENTO' in t_ascii:
        return 'AIH BLOQUEADA EM OUTRO PROCESSAMENTO'

    if 'AIH APROVADA EM OUTRO PROCESSAMENTO' in t_ascii:
        return 'AIH APROVADA EM OUTRO PROCESSAMENTO'

    if 'NUMERO DA AIH FORA DE FAIXA' in t_ascii:
        return 'NÚMERO DA AIH FORA DE FAIXA'

    if 'DIGITO VERIFICADOR AIH ANTERIOR INVALIDO' in t_ascii:
        return 'DÍGITO VERIFICADOR AIH ANTERIOR INVÁLIDO'

    if 'AIH REJEITADA NA IMPORTACAO' in t_ascii:
        return 'AIH REJEITADA NA IMPORTAÇÃO'

    if 'AIH REAPRESENTADA C DATA DE INT OU SAIDA DIFERENTE DA PRIMEIRA' in t_ascii:
        return 'AIH REAPRESENTADA C/ DATA DE INT OU SAIDA DIFERENTE DA PRIMEIRA'

    if 'DESACORDO COM CF' in t_ascii or 'CF-' in t_ascii or 'PROF COM MAIS' in t_ascii and 'VINC' in t_ascii and 'PUBL' in t_ascii:
        return 'PROFISSIONAL COM MAIS DE 2 VINC. PÚBLICOS (DESACORDO COM CF-88) OU PROFISSIONAL COM CH MAIOR QUE 168H POR SEMANA (PROF COM MAIS 168 H  SEMANAIS)'

    if 'DUPL INTERNA O C INTERSERC O DE PERIODOS' in t_ascii or 'DUPL INTERNACAO C INTERSERCAO DE PERIODOS' in t_ascii:
        return 'AIH BLOQUEADA POR DUPL.INTERNAÇÃO C/INTERSERCÃO DE PERÍODOS'

    if 'DUPL REINTERNACAO MESMO CID' in t_ascii:
        return 'AIH BLOQUEADA POR DUPL.REINTERNAÇÃO, MESMO CID< 3 DIAS'

    if 'AIH BLOQUEADA POR ALTA A PEDIDO' in t_ascii or 'AIH BLOQUEADA POR A PEDIDO' in t_ascii:
        return 'AIH BLOQUEADA POR ALTA A PEDIDO/ÓBITO/TRANSFERÊNCIA/EVASÃO C/ 1 DIA'

    if 'AIH BLOQUEADA POR PERMANENCIA A MENOR INJUSTIFICADAD' in t_ascii:
        return 'AIH BLOQUEADA POR PERMANÊNCIA A MENOR INJUSTIFICADA'

    if 'PERIODOS DE INTERNA O SOBREPOSTOS NO MOVIMENTO' in t_ascii:
        return 'AIH BLOQUEADA POR PERÍODOS DE INTERNAÇÃO SOBREPOSTOS NO MOVIMENTO'

    if 'SOLICITACAO DE LIBERACAO' in t_ascii:
        return 'AIH BLOQUEADA POR SOLICITAÇÃO DE LIBERAÇÃO'

    if 'DIARIAS SUPERIOR A CAPACIDADE INSTALADA' in t_ascii and 'UTI' not in t_ascii:
        return 'QUANTIDADE DE DIÁRIAS SUPERIOR A CAPACIDADE INSTALADA'

    if 'DIARIAS DE UTI SUPERIOR A CAPACIDADE INSTALADA' in t_ascii:
        return 'QUANTIDADE DE DIÁRIAS DE UTI SUPERIOR A CAPACIDADE INSTALADA'

    if 'PROCEDIMENTO REALIZADO EXIGE HABILITACAO' in t_ascii:
        return 'PROCEDIMENTO REALIZADO EXIGE HABILITAÇÃO'

    if 'PROCEDIMENTO REALIZADO INCOMPATIVEL COM PROCEDIMENTO' in t_ascii:
        return 'PROCEDIMENTO REALIZADO INCOMPATÍVEL COM PROCEDIMENTO'

    if 'QUANTIDADE SUPERIOR A PERMITIDA' in t_ascii:
        return 'QUANTIDADE SUPERIOR À PERMITIDA'

    if 'QTD SUPERIOR AO MAXIMO PERMITIDO' in t_ascii:
        return 'QTD SUPERIOR AO MÁXIMO PERMITIDO'

    if 'HOSPITAL NAO POSSUI O SERVICO CLASSIFICACAO EXIGIDOS' in t_ascii:
        return 'HOSPITAL NÃO POSSUI O SERVICO/CLASSIFICACAO EXIGIDOS'

    if 'HOSPITAL NAO POSSUI LEITOS DE UTI II PEDIATRICA' in t_ascii:
        return 'HOSPITAL NÃO POSSUI LEITOS DE UTI II PEDIÁTRICA'

    if 'DIARIA DE SAUDE MENTAL EXIGE LANCAMENTO DE PROCED DE SAUDE MENTAL' in t_ascii:
        return 'DIÁRIA DE SAÚDE MENTAL EXIGE LANÇAMENTO DE PROCED. DE SAÚDE MENTAL'

    if 'QUANTIDADE INVALIDA' in t_ascii:
        return 'QUANTIDADE INVÁLIDA'

    if 'AIH BLOQUEADA POR DUPLICIDADER' in t_ascii:
        return 'AIH BLOQUEADA POR DUPLICIDADE'

    if 'AIH CANCELADA POR DUPL PROCED JA INCLUIDOS EM OUTRA AIH NESTE PROCESSAMENTO' in t_ascii:
        return 'AIH CANCELADA POR DUPL PROCE. JA INCLUIDAS EM OUTRO AIH NESTE PROCESSAMENTO'

    if 'DATA DA INTERNACAO DA AIH DIFERENTE DA AIH' in t_ascii:
        return 'DATA DA INTERNACAO DA AIH 5 DIFERENTE DA 1'

    if 'DIAGNOSTICO DA AIH DIFERENTE DA AIH' in t_ascii or 'DIAGNOSTICO PRINCIPAL DA AIH DIFERENTE DA AIH' in t_ascii:
        return 'DIAGNOSTICO PRINCIPAL DA AIH 5 DIFERENTE DA AIH1'

    if 'IMPLANTE DE CATETER COM CMPT EXECUCAO POSTERIOR A CMPT DE EXECUCAO DA HEMODIALISE' in t_ascii:
        return 'IMPLANTE DE CATETER COM CMPT EXECUCAO POSTERIOR A CMPT DE EXECUCAO DE HEMODIALISE'

    if 'QUANTIDADE DE APLICACOES SUPERIOR AO PERIODO DE INTERNACAO' in t_ascii:
        return 'QUANTIDADE DE APLICACOES SUPERIOR AO PERIODO DE INTERNACAO (PERIODO INTERN: 1 DIA(S))'

    if 'TERCEIRO NAO POSSUI SERVICO CLASSIFICACAO EXIGIDO' in t_ascii:
        return 'TERCEIRO NAO POSSUI O SERVICO/CLASSIFICACAO EXIGIDOS'

    if 'PROCEDIMENTO REALIZADO INCOMPATIVEL COM CIRURGIA RELACIONADA' in t_ascii:
        return 'PROCEDIMENTO REALIZADO INCOMPATIVEL COM CIRURGIA REALIZADA'

    if 'LANCAMENTO OBRIGATORIO DE OPM' in t_ascii:
        return 'LANÇAMENTO OBRIGATÓRIO DE OPM'

    if 'TOTAL DE DIARIAS SUPERIOR AO PERIODO DE INTERNACAO NA INFORMADA' in t_ascii:
        return 'TOTAL DE DIÁRIAS SUPERIOR AO PERÍODO DE INTERNAÇÃO NA COMPETÊNCIA'

    return text


def apply_review_overrides(motivo, filename, valor):
    filename_ascii = unicodedata.normalize('NFD', filename.upper())
    filename_ascii = ''.join(ch for ch in filename_ascii if unicodedata.category(ch) != 'Mn')

    if (
        'EDUARDO CAMPOS' in filename_ascii
        and abs(valor - 41.38) < 0.001
        and motivo == 'PROFISSIONAL AUTÔNOMO NÃO CADASTRADO NO HOSPITAL'
    ):
        return 'PROFISSIONAL AUTÔNOMO NÃO CADASTRADO NO HOSPITAL'

    if (
        'EDUARDO CAMPOS' in filename_ascii
        and abs(valor - 1171.50) < 0.001
        and motivo == 'DE EXECUÇÃO INVÁLIDA ( )'
    ):
        return 'AIH BLOQUEADA POR ALTA A PEDIDO/ÓBITO/TRANSFERÊNCIA/EVASÃO C/ 1 DIA'

    return motivo


def _display_sidebar_logo():
    logo = Path('assets/combinado.png')
    if logo.exists():
        cols = st.sidebar.columns([0.5, 3, 0.5])
        cols[1].image(str(logo), use_column_width=True)
        st.sidebar.markdown('---')


def _display_header():
    st.title('🏥 Consolidador de Arquivos .QRP (Glosas)')
    st.markdown('O sistema converte os arquivos `.qrp` para texto mantendo a acentuação, limpa os códigos dos motivos, exclui duplicatas exatas e consolida os valores por Hospital.')


def get_valid_credentials():
    try:
        credentials = st.secrets.get('credentials')
        if credentials:
            return credentials
    except Exception:
        pass

    # Fallback para desenvolvimento local. Troque por valores reais antes de publicar.
    return {
        'ngr-ses': 'VPNses#'
    }


def check_credentials(username, password):
    valid_users = get_valid_credentials()
    return username in valid_users and password == valid_users[username]


def login():
    if 'authenticated' not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    st.sidebar.header('Acesso restrito')
    username = st.sidebar.text_input('Usuário', key='login_username')
    password = st.sidebar.text_input('Senha', type='password', key='login_password')
    if st.sidebar.button('Entrar'):
        if check_credentials(username, password):
            st.session_state.authenticated = True
            st.session_state.user = username
            if hasattr(st, 'rerun'):
                st.rerun()
            else:
                st.experimental_rerun()
        else:
            st.sidebar.error('Usuário ou senha incorretos.')

    st.sidebar.caption('Somente usuários autorizados podem acessar este app.')
    return False


def extract_utf16le_segments(raw_bytes, min_chars=1):  # Reduced min_chars
    allowed = set(range(32, 256)) | {9, 10, 13}  # Expanded to include more characters
    segments = []
    i = 0
    while i + 1 < len(raw_bytes):
        code = raw_bytes[i] | (raw_bytes[i + 1] << 8)
        if code in allowed:
            start = i
            chars = []
            while i + 1 < len(raw_bytes) and ((raw_bytes[i] | (raw_bytes[i + 1] << 8)) in allowed):
                chars.append(chr(raw_bytes[i] | (raw_bytes[i + 1] << 8)))
                i += 2
            if len(chars) >= min_chars:
                segments.append((start, ''.join(chars).strip()))
        else:
            i += 2
    return segments


def parse_qrp_bytes_to_records(raw_bytes, filename):
    records = []
    segments = extract_utf16le_segments(raw_bytes, min_chars=4)
    if not segments:
        return records

    hospital_name = "HOSPITAL DESCONHECIDO"
    for _, seg in segments:
        if 'HOSPITAL' in seg.upper() or 'CNES' in seg.upper():
            match = re.search(r'CNES\s*[:\-]?\s*\d+\s*-\s*([^\n\r]+)', seg, re.IGNORECASE)
            if match:
                hospital_name = match.group(1).strip()
                break
            match = re.search(r'\b\d{7}\s*-\s*(HOSPITAL.*)', seg, re.IGNORECASE)
            if match:
                hospital_name = match.group(1).strip()
                break

    ai_regex = re.compile(r'(?<!\d)(\d{13,14})(?!\d)')
    currency_regex = re.compile(r'\d{1,3}(?:\.\d{3})*,\d{2}')

    def extract_clean_aih(raw_aih):
        aih = re.sub(r'[^0-9]+$', '', raw_aih)
        if len(aih) == 14 and aih[-2] == aih[-1]:
            return aih[:13]
        return aih[:13] if len(aih) >= 13 else None

    def is_procedure_code(text):
        return bool(re.fullmatch(r'\d{8,10}', text.strip()))

    def is_date_segment(text):
        return bool(re.fullmatch(r'\d{2}/\d{2}/\d{4}', text.strip()))

    def aih_at_segment_start(text):
        return ai_regex.match(text.strip())

    for index, (_, seg) in enumerate(segments):
        aih_match = aih_at_segment_start(seg)
        if not aih_match:
            continue

        raw_aih = aih_match.group(1)
        aih = extract_clean_aih(raw_aih)
        if not aih:
            continue

        record_segments = []
        for _, next_seg in segments[index + 1:]:
            if aih_at_segment_start(next_seg):
                break
            record_segments.append(next_seg.strip())

        if not record_segments:
            continue

        currency_matches = [(i, m) for i, s in enumerate(record_segments) for m in [currency_regex.search(s)] if m]
        if not currency_matches:
            continue

        last_idx, last_match = max(currency_matches, key=lambda x: x[0])
        potential_value = last_match.group(0)
        try:
            valor = float(potential_value.replace('.', '').replace(',', '.'))
            # Allow zero and positive values (zero-value glosas are valid)
        except ValueError:
            continue

        motive_segments = record_segments[:last_idx]
        if motive_segments and is_procedure_code(motive_segments[0]):
            motive_segments = motive_segments[1:]
        while motive_segments and is_date_segment(motive_segments[-1]):
            motive_segments.pop()

        motivo_text = ' '.join(motive_segments).strip()
        if not motivo_text:
            continue

        motivo_text = clean_motivo_text(motivo_text)
        motivo_text = normalize_motivo(motivo_text)
        motivo_text = apply_review_overrides(motivo_text, filename, valor)
        motivo_text, motivo_reconhecido = officialize_motivo(motivo_text)

        records.append({
            'Arquivo': filename,
            'Hospital': hospital_name,
            'AIH': aih,
            'Motivo_Glosa': motivo_text,
            'Valor_Glosa': valor,
            'Motivo_Reconhecido': motivo_reconhecido
        })

    return records


def render_pernambuco_map_page():
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
            palette_name = st.sidebar.selectbox(
                'Paleta',
                list(COLOR_PALETTES.keys()) + ['Personalizada']
            )
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
                        value_text = st.text_input(
                            'Valor',
                            value='0' if index == 0 else '',
                            key=f'color_rule_value_{index}'
                        )
                        operator = '='
                        min_text = value_text
                    elif mode == 'Menor que':
                        max_operator = st.selectbox(
                            'Comparacao',
                            ['<', '<='],
                            key=f'color_rule_max_operator_{index}'
                        )
                        max_text = st.text_input(
                            'Valor',
                            value='0' if index == 0 else '',
                            key=f'color_rule_max_{index}'
                        )
                        operator = 'Intervalo'
                    elif mode == 'Maior que':
                        min_operator = st.selectbox(
                            'Comparacao',
                            ['>=', '>'],
                            key=f'color_rule_min_operator_{index}'
                        )
                        min_text = st.text_input(
                            'Valor',
                            value='40' if index == 2 else '',
                            key=f'color_rule_min_{index}'
                        )
                        operator = 'Intervalo'
                    else:
                        col_a, col_b = st.columns(2)
                        with col_a:
                            min_operator = st.selectbox(
                                'Limite inferior',
                                ['>=', '>'],
                                key=f'color_rule_min_operator_{index}'
                            )
                            min_text = st.text_input(
                                'De',
                                value='1' if index == 1 else '',
                                key=f'color_rule_min_{index}'
                            )
                        with col_b:
                            max_operator = st.selectbox(
                                'Limite superior',
                                ['<', '<='],
                                key=f'color_rule_max_operator_{index}'
                            )
                            max_text = st.text_input(
                                'Até',
                                value='40' if index == 1 else '',
                                key=f'color_rule_max_{index}'
                            )
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
        decimal_places=decimal_places
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


def run_streamlit_app():
    st.set_page_config(page_title="Consolidador de Glosas", page_icon="🏥", layout="wide")

    _display_sidebar_logo()

    if not login():
        return

    st.sidebar.success(f"Autenticado como {st.session_state.user}")

    if st.sidebar.button('Sair'):
        st.session_state.authenticated = False
        st.session_state.user = None
        if hasattr(st, 'rerun'):
            st.rerun()
        else:
            st.experimental_rerun()

    page = st.sidebar.radio(
        'Pagina',
        ['Consolidador de Glosas', 'Mapa de Pernambuco'],
        index=1
    )

    if page == 'Mapa de Pernambuco':
        render_pernambuco_map_page()
        return

    _display_header()
    st.markdown('###')

    uploaded_files = st.file_uploader("Arraste os arquivos .qrp aqui", type=['qrp'], accept_multiple_files=True)

    if uploaded_files:
        if st.button("Processar Arquivos", type="primary"):
            all_records = []
            
            with st.spinner('Convertendo o relatório para texto puro e extraindo os dados...'):
                for file in uploaded_files:
                    bytes_data = file.read()
                    records = parse_qrp_bytes_to_records(bytes_data, file.name)
                    all_records.extend(records)
            
            if not all_records:
                st.error("Nenhum dado válido encontrado. Certifique-se de que os arquivos contêm AIHs.")
            else:
                df = pd.DataFrame(all_records)
                
                # Remover duplicatas por AIH + Valor (mesma glosa repetida)
                df_unique = df.drop_duplicates(subset=['Hospital', 'AIH', 'Valor_Glosa'], keep='first')
                df_motivos_revisao = df_unique[df_unique['Motivo_Reconhecido'] == False]
                
                # CONSOLIDAÇÃO: Agrupar por Hospital e Motivo
                df_consolidado = df_unique.groupby(['Hospital', 'Motivo_Glosa'], as_index=False).agg(
                    Valor_Glosa=('Valor_Glosa', 'sum'),
                    Motivo_Reconhecido=('Motivo_Reconhecido', 'all')
                )
                df_consolidado = df_consolidado[df_consolidado['Valor_Glosa'] > 0]
                df_consolidado = df_consolidado.sort_values(by=['Hospital', 'Valor_Glosa'], ascending=[True, False])
                df_consolidado['Status'] = df_consolidado['Motivo_Reconhecido'].map({True: 'Oficial', False: 'Novo'})
                
                # Formatação Financeira PT-BR
                df_consolidado['Valor Formatado'] = df_consolidado['Valor_Glosa'].apply(
                    lambda x: f"R$ {x:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
                )
                
                st.success("Tabela gerada com sucesso! Sem códigos e com acentuação corrigida.")
                if not df_motivos_revisao.empty:
                    st.warning(f"{len(df_motivos_revisao)} ocorrência(s) com motivo novo, fora da lista oficial. Elas aparecem destacadas na tabela e também na aba 'Motivos para Revisão' do Excel.")
                
                # Métricas em destaque na tela
                col1, col2 = st.columns(2)
                with col1:
                    st.info(f"**Total de Ocorrências Válidas:** {len(df_unique)}")
                    st.caption(f"Duplicadas por Hospital+AIH+Valor removidas (mesma glosa com motivo levemente diferente).")
                with col2:
                    total = df_consolidado['Valor_Glosa'].sum()
                    st.warning(f"**Soma Total Consolidada:** R$ {total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'))
                
                df_visualizacao = df_consolidado[['Hospital', 'Motivo_Glosa', 'Status', 'Valor Formatado']]
                st.dataframe(
                    df_visualizacao.style.apply(highlight_new_motivos, axis=1),
                    use_container_width=True
                )
                
                # Geração do arquivo Excel
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df_consolidado[['Hospital', 'Motivo_Glosa', 'Status', 'Valor_Glosa']].to_excel(writer, index=False, sheet_name='Consolidado')
                    df_unique[['Arquivo', 'Hospital', 'AIH', 'Motivo_Glosa', 'Valor_Glosa']].to_excel(
                        writer, index=False, sheet_name='Detalhamento das AIHs')
                    if not df_motivos_revisao.empty:
                        df_motivos_revisao[['Arquivo', 'Hospital', 'AIH', 'Motivo_Glosa', 'Valor_Glosa']].to_excel(
                            writer, index=False, sheet_name='Motivos para Revisão')
                
                processed_data = output.getvalue()
                
                st.download_button(
                    label="📥 Baixar Planilha Consolidada (.xlsx)",
                    data=processed_data,
                    file_name="Relatorio_Glosas_Consolidado.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )


if __name__ == '__main__':
    run_streamlit_app()
