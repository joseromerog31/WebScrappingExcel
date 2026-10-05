import pandas as pd
import sys
import ctypes
from pathlib import Path
from tkinter import Tk, filedialog
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from urllib.parse import quote_plus, unquote
import re
import time
import unicodedata

# Mejorar nitidez de Tkinter en pantallas con escalado de Windows
if sys.platform == 'win32':
    try:
        # Windows 10/11 - Per Monitor DPI Awareness
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)
        )
    except Exception:
        try:
            # Fallback para versiones anteriores de Windows
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()

# Crear Tkinter después de configurar el DPI
root = Tk()
root.withdraw()
root.attributes('-topmost', True)

# El usuario selecciona el archivo
selected_file = filedialog.askopenfilename(
    title='Selecciona el archivo de hospitales',
    filetypes=[
        ('Archivos de Excel', '*.xlsx *.xls'),
        ('Todos los archivos', '*.*')
    ]
)

root.destroy()

# Si el usuario cierra la ventana sin elegir archivo
if not selected_file:
    raise ValueError('No se seleccionó ningún archivo.')

input_path = Path(selected_file)

print(f'Archivo seleccionado: {input_path.name}')
print(f'Ruta: {input_path}')

# Leer Excel
locations = pd.read_excel(input_path)

print('Columnas encontradas:', locations.columns.tolist())

# Validar columnas obligatorias
required_columns = ['Nombre', 'País LATAM']

missing_columns = [
    col for col in required_columns
    if col not in locations.columns
]

if missing_columns:
    raise ValueError(
        'El archivo debe contener las columnas requeridas: '
        + ', '.join(missing_columns)
    )

print(locations.head())

# Cambiar a False para hacer pruebas si se requiere
HEADLESS = True

options = webdriver.ChromeOptions()
if HEADLESS:
    options.add_argument('--headless=new')

options.add_argument('--window-size=1920,1080')
options.add_argument('--lang=es')

driver = webdriver.Chrome(options=options)

def clean_value(value):
    if pd.isna(value):
        return ''
    return str(value).strip()


# Configuración postal para los 8 países soportados.
# Las llaves se guardan sin acentos porque normalize_country() normaliza el valor del Excel.
COUNTRY_CONFIG = {
    'argentina': {
        'canonical_name': 'Argentina',
        'postal_patterns': [
            r'\b[A-Z]\d{4}[A-Z]{3}\b'
        ]
    },
    'colombia': {
        'canonical_name': 'Colombia',
        'postal_patterns': [
            r'(?<!\d)\d{6}(?!\d)'
        ]
    },
    'mexico': {
        'canonical_name': 'México',
        'postal_patterns': [
            r'(?<!\d)\d{5}(?!\d)'
        ]
    },
    'peru': {
        'canonical_name': 'Perú',
        'postal_patterns': [
            r'(?<!\d)\d{5}(?!\d)'
        ]
    },
    'paraguay': {
        'canonical_name': 'Paraguay',
        'postal_patterns': [
            r'(?<!\d)\d{6}(?!\d)'
        ]
    },
    'chile': {
        'canonical_name': 'Chile',
        'postal_patterns': [
            r'(?<!\d)\d{7}(?!\d)'
        ]
    },
    'ecuador': {
        'canonical_name': 'Ecuador',
        'postal_patterns': [
            r'(?<!\d)\d{6}(?!\d)'
        ]
    },
    'uruguay': {
        'canonical_name': 'Uruguay',
        'postal_patterns': [
            r'(?<!\d)\d{5}(?!\d)'
        ]
    }
}


def normalize_country(value):
    # Normaliza mayúsculas, espacios y acentos del país leído desde Excel.
    value = clean_value(value).lower()
    value = unicodedata.normalize('NFD', value)
    value = ''.join(
        char for char in value
        if unicodedata.category(char) != 'Mn'
    )
    return value.strip()


def get_country_config(country):
    # Devuelve la configuración del país o None si no está soportado.
    return COUNTRY_CONFIG.get(normalize_country(country))


def build_search_query(row):
    # Encabezados Excel
    parts = [
        clean_value(row.get('Nombre', '')),
        clean_value(row.get('Ciudad', '')),
        clean_value(row.get('Estado / Provincia / Departamento', '')),
        clean_value(row.get('País LATAM', ''))
    ]
    return ' '.join(x for x in parts if x)


def detect_captcha(driver):
    try:
        body = driver.find_element(By.TAG_NAME, 'body').text.lower()
    except Exception:
        return False

    indicators = [
        'unusual traffic',
        'tráfico inusual',
        'captcha',
        'nuestros sistemas han detectado'
    ]
    return any(text in body for text in indicators)


def dismiss_consent(driver):
    # Solo cierra el consentimiento normal de Google. No se salta CAPTCHA.
    texts = ['Aceptar todo', 'Accept all']
    for text in texts:
        try:
            buttons = driver.find_elements(
                By.XPATH,
                f"//button[contains(., '{text}')]"
            )
            if buttons and buttons[0].is_displayed():
                buttons[0].click()
                time.sleep(0.5)
                return
        except Exception:
            pass


def detail_panel_is_open(driver):
    for selector in ['h1.DUwDvf', 'h1[class*="fontHeadlineLarge"]']:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.text.strip():
                    return True
        except Exception:
            pass
    return False


def open_place_detail(driver, timeout=8):
    if detail_panel_is_open(driver):
        return True, driver.current_url

    end = time.time() + timeout
    selectors = [
        'a.hfpxzc',
        'div[role="feed"] a[href*="/maps/place/"]'
    ]

    while time.time() < end:
        if detail_panel_is_open(driver):
            return True, driver.current_url

        for selector in selectors:
            try:
                results = [
                    x for x in driver.find_elements(By.CSS_SELECTOR, selector)
                    if x.is_displayed()
                ]
                if results:
                    first = results[0]
                    result_url = first.get_attribute('href')

                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        first
                    )
                    first.click()

                    try:
                        WebDriverWait(driver, 7).until(
                            lambda d: detail_panel_is_open(d)
                        )
                    except TimeoutException:
                        pass

                    # Margen de tiempo para actualizar
                    try:
                        WebDriverWait(driver, 4).until(
                            lambda d: (
                                '!3d' in d.current_url and '!4d' in d.current_url
                            ) or '/maps/place/' in d.current_url
                        )
                    except TimeoutException:
                        pass

                    return detail_panel_is_open(driver), result_url
            except Exception:
                pass

        time.sleep(0.4)

    return False, None

def get_google_name(driver):
    for selector in ['h1.DUwDvf', 'h1[class*="fontHeadlineLarge"]']:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for element in elements:
                value = element.text.strip()
                if element.is_displayed() and value:
                    return value
        except Exception:
            pass
    return 'Name not found'


def get_address(driver):
    selectors = [
        'button[data-item-id="address"]',
        '[data-item-id="address"]',
        'button[aria-label^="Dirección:"]',
        'button[aria-label^="Address:"]'
    ]

    # Espera corta: dirección suele cargar apenas abre la ficha.
    end = time.time() + 3
    while time.time() < end:
        for selector in selectors:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    if not element.is_displayed():
                        continue

                    value = (
                        element.get_attribute('aria-label')
                        or element.text
                        or ''
                    ).strip()

                    value = re.sub(
                        r'^(Address|Dirección):\s*',
                        '',
                        value,
                        flags=re.IGNORECASE
                    ).strip()

                    if value:
                        return value
            except Exception:
                pass
        time.sleep(0.3)

    return 'Address not found'


def extract_postal_code(address, country):
    # Extrae el código postal usando únicamente el formato del país de la fila.
    if not address or address == 'Address not found':
        return 'Postal code not found'

    config = get_country_config(country)
    if config is None:
        return 'Unsupported country'

    for pattern in config['postal_patterns']:
        matches = re.findall(pattern, address, flags=re.IGNORECASE)
        if matches:
            # Usamos la última coincidencia porque en Google Maps el CP suele aparecer hacia el final de la dirección, antes de ciudad/estado/país.
            match = matches[-1]
            if isinstance(match, tuple):
                match = ''.join(match)
            return str(match).upper()

    return 'Postal code not found'


def valid_coordinates(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        return -90 <= lat <= 90 and -180 <= lon <= 180
    except (TypeError, ValueError):
        return False


def exact_coordinates_from_url(url):
    if not url:
        return None, None

    # Selenium devuelve el href decodificado, pero unquote también cubre URLs donde !3d / !4d vengan escapados como %21.
    decoded = unquote(str(url))
    match = re.search(
        r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)',
        decoded
    )
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if valid_coordinates(lat, lon):
            return lat, lon
    return None, None

def viewport_coordinates(url):
    if not url:
        return None, None

    decoded = unquote(str(url))
    match = re.search(
        r'@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)',
        decoded
    )
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if valid_coordinates(lat, lon):
            return lat, lon
    return None, None

def structured_coordinates_from_page(driver):
    try:
        source = driver.page_source
    except Exception:
        return []

    if not source:
        return []

    # Maps serializa información varias veces y puede escapar comillas/unicode. Normalizar antes de buscar coordenadas.
    for _ in range(2):
        source = source.replace('\\\"', '"').replace('\\"', '"')

    replacements = {
        r'\u0022': '"',
        r'\u003d': '=',
        r'\u0026': '&',
        '&quot;': '"',
        '&#34;': '"'
    }
    for old, new_value in replacements.items():
        source = source.replace(old, new_value)

    candidates = []
    patterns = [
        ('latlon', r'"latitude"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"longitude"\s*:\s*(-?\d+(?:\.\d+)?)'),
        ('latlon', r'"latitude"\s*:\s*(-?\d+(?:\.\d+)?).{0,180}?"longitude"\s*:\s*(-?\d+(?:\.\d+)?)'),
        ('lonlat', r'"longitude"\s*:\s*(-?\d+(?:\.\d+)?).{0,180}?"latitude"\s*:\s*(-?\d+(?:\.\d+)?)')
    ]

    for order, pattern in patterns:
        for match in re.finditer(pattern, source, flags=re.DOTALL):
            if order == 'latlon':
                lat, lon = float(match.group(1)), float(match.group(2))
            else:
                lon, lat = float(match.group(1)), float(match.group(2))
            if valid_coordinates(lat, lon):
                candidates.append((lat, lon))

    for match in re.finditer(
        r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)',
        source
    ):
        lat, lon = float(match.group(1)), float(match.group(2))
        if valid_coordinates(lat, lon):
            candidates.append((lat, lon))

    unique = []
    seen = set()
    for lat, lon in candidates:
        key = (round(lat, 7), round(lon, 7))
        if key not in seen:
            seen.add(key)
            unique.append((lat, lon))

    return unique

def choose_best_candidate(candidates, driver):
    # Elige el candidato del HTML más cercano al centro visible del mapa.
    # El viewport se usa solamente para escoger entre coordenadas ya encontradas en la ficha.
    if not candidates:
        return None, None

    if len(candidates) == 1:
        return candidates[0]

    center_lat, center_lon = viewport_coordinates(driver.current_url)
    if center_lat is None or center_lon is None:
        return candidates[0]

    def squared_distance(candidate):
        lat, lon = candidate
        return (lat - center_lat) ** 2 + (lon - center_lon) ** 2

    return min(candidates, key=squared_distance)


def get_coordinates(driver, result_url=None, timeout=6):
    # Obtiene coordenadas sin realizar una segunda búsqueda

    # 1) El href del resultado que acabamos de abrir suele ser la fuente más rápida y confiable.
    lat, lon = exact_coordinates_from_url(result_url)
    if lat is not None:
        return lat, lon, 'RESULT_LINK_EXACT'

    # 2) Revisar la URL actual. A veces Google tarda.
    end = time.time() + timeout
    last_candidates = []

    while time.time() < end:
        lat, lon = exact_coordinates_from_url(driver.current_url)
        if lat is not None:
            return lat, lon, 'CURRENT_URL_EXACT'

        candidates = structured_coordinates_from_page(driver)
        if candidates:
            last_candidates = candidates

        time.sleep(0.4)

    # 3) Si la URL nunca expuso !3d / !4d, usamos las coordenadas del HTML y elegimos la más cercana al centro visible del mapa.
    if last_candidates:
        lat, lon = choose_best_candidate(last_candidates, driver)
        return lat, lon, 'PAGE_SOURCE'

    # 4) LAT y LON solo es el centro del mapa. Lo conservamos únicamente para revisión manual, no como coordenada exacta confirmada.
    for candidate_url in [driver.current_url, result_url]:
        lat, lon = viewport_coordinates(candidate_url)
        if lat is not None:
            return lat, lon, 'VIEWPORT_REVIEW'

    return None, None, 'NOT_FOUND'

def search_google_maps(driver, row):
    query = build_search_query(row)
    country_input = clean_value(row.get('País LATAM', ''))
    country_normalized = normalize_country(country_input)
    country_config = get_country_config(country_input)

    result = {
        'Google_Name': '',
        'Google_Address': '',
        'Postal_Code': '',
        'Latitude': None,
        'Longitude': None,
        'Coordinate_Source': 'NOT_FOUND',
        'Search_Query': query,
        'Country_Normalized': country_normalized,
        'Search_Status': 'EMPTY INPUT'
    }

    if not query:
        return result

    # No intentamos inferir reglas postales de países fuera del alcance definido.
    if country_config is None:
        result['Search_Status'] = 'REVIEW - UNSUPPORTED COUNTRY'
        print(f'  -> País no soportado: {country_input}')
        return result

    print(f"Buscando: {query} [{country_config['canonical_name']}]")

    try:
        # UNA sola búsqueda de Google Maps por fila.
        url = 'https://www.google.com/maps/search/?api=1&query=' + quote_plus(query)
        driver.get(url)

        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body'))
        )
        dismiss_consent(driver)

        if detect_captcha(driver):
            result['Search_Status'] = 'STOP - CAPTCHA / MANUAL REVIEW'
            return result

        # Si Maps muestra una lista, esto solo abre el primer resultado.
        place_opened, result_url = open_place_detail(driver)
        if not place_opened:
            result['Search_Status'] = 'NO PLACE DETAIL FOUND'
            return result

        google_name = get_google_name(driver)
        address = get_address(driver)
        postal_code = extract_postal_code(address, country_input)
        latitude, longitude, source = get_coordinates(
            driver,
            result_url=result_url
        )

        if latitude is None or longitude is None:
            print('  URL del resultado:', result_url)
            print('  URL actual:', driver.current_url)

        if google_name == 'Name not found' and address == 'Address not found':
            status = 'REVIEW - PLACE DETAIL INCOMPLETE'
        elif address == 'Address not found':
            status = 'REVIEW - ADDRESS NOT FOUND'
        elif latitude is None or longitude is None:
            status = 'ADDRESS FOUND - COORDINATES REVIEW'
        elif source == 'VIEWPORT_REVIEW':
            status = 'COORDINATES FROM VIEWPORT - REVIEW'
        elif postal_code == 'Unsupported country':
            status = 'REVIEW - UNSUPPORTED COUNTRY'
        elif postal_code == 'Postal code not found':
            status = 'ADDRESS FOUND - CP REVIEW'
        else:
            status = 'OK'

        result.update({
            'Google_Name': google_name,
            'Google_Address': address,
            'Postal_Code': postal_code,
            'Latitude': latitude,
            'Longitude': longitude,
            'Coordinate_Source': source,
            'Search_Status': status
        })

        print(
            f"  -> {status} | {google_name} | "
            f"{latitude}, {longitude} [{source}]"
        )

        return result

    except Exception as e:
        print(f'  -> Error: {type(e).__name__}: {e}')
        result['Google_Address'] = 'Error'
        result['Search_Status'] = f'ERROR - {type(e).__name__}'
        return result

# Procesar cada fila UNA sola vez.

result_columns = [
    'Google_Name',
    'Google_Address',
    'Postal_Code',
    'Latitude',
    'Longitude',
    'Coordinate_Source',
    'Search_Query',
    'Country_Normalized',
    'Search_Status'
]

output_path = input_path.with_name(
    f'{input_path.stem}_DireccionesRevisadas.xlsx'
)

try:
    for position, (idx, row) in enumerate(locations.iterrows(), start=1):
        print(f'[{position}/{len(locations)}]')
        result = search_google_maps(driver, row)

        # Guardar directamente en la misma fila.
        for column in result_columns:
            locations.loc[idx, column] = result[column]

        # Si aparece CAPTCHA, conserva lo obtenido hasta ese momento y se detiene.
        if result['Search_Status'].startswith('STOP - CAPTCHA'):
            print(f'Proceso detenido en la fila {idx} por CAPTCHA.')
            break

        # Pausa corta entre lugares.
        time.sleep(1.0)

finally:
    # Guardar incluso si hubo CAPTCHA o algún error inesperado.
    locations.to_excel(
        output_path,
        index=False
    )

    driver.quit()

print(f'Archivo guardado en: {output_path}')

locations[
    [
        'Nombre',
        'País LATAM',
        'Country_Normalized',
        'Google_Name',
        'Google_Address',
        'Postal_Code',
        'Latitude',
        'Longitude',
        'Coordinate_Source',
        'Search_Status'
    ]
].head(20)

