from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

import pandas as pd

COL_TERRITORIO = 'TERRITORIO FINAL'
COL_PRODUCTO = 'NOMBRE UBICACION PRODUCTO'
COL_DEPTO = 'DEPARTAMENTO ENCONTRADO'
COL_ESTATUS = 'ESTATUS'
COLUMNAS = [COL_TERRITORIO, COL_PRODUCTO, COL_DEPTO, COL_ESTATUS]

# Dataset DIVIPOLA, generado a partir de datos DANE publicados en datos.gov.co.
# Incluye municipios, islas y áreas no municipalizadas.
DANE_ENDPOINT = 'https://www.datos.gov.co/resource/gdxc-w37w.json'
CACHE_DIAS = 30
MAX_INTENTOS_MAPS = 3
PAUSA_MAPS = 1.2
GUARDAR_CADA = 10
HEADLESS = True

# Semilla de respaldo cuando la API no responde. No pretende cubrir el país: para cualquier otro municipio se requiere descargar el catálogo completo.
# Ciudades en la muestra del usuario, con su departamento correspondiente.
SEMILLA = {
    'ITAGUI': 'ANTIOQUIA', 'PITALITO': 'HUILA', 'BELLO': 'ANTIOQUIA',
    'SOLEDAD': 'ATLÁNTICO', 'SOACHA': 'CUNDINAMARCA',
    'VILLETA': 'CUNDINAMARCA', 'CAREPA': 'ANTIOQUIA',
    'PALMIRA': 'VALLE DEL CAUCA', 'BOGOTA': 'CUNDINAMARCA',
    'POPAYAN': 'CAUCA', 'VILLAVICENCIO': 'META', 'IBAGUE': 'TOLIMA',
    'BARRANCABERMEJA': 'SANTANDER', 'APARTADO': 'ANTIOQUIA',
    'MEDELLIN': 'ANTIOQUIA', 'COCORNA': 'ANTIOQUIA',
    'NECHI': 'ANTIOQUIA', 'YOLOMBO': 'ANTIOQUIA',
    'ANDES': 'ANTIOQUIA', 'YARUMAL': 'ANTIOQUIA',
    'ANGOSTURA': 'ANTIOQUIA', 'PEQUE': 'ANTIOQUIA',
    'PUEBLORRICO': 'ANTIOQUIA', 'VIGIA DEL FUERTE': 'ANTIOQUIA',
    'EBEJICO': 'ANTIOQUIA', 'JARDIN': 'ANTIOQUIA',
    'MONTEBELLO': 'ANTIOQUIA', 'SAN ROQUE': 'ANTIOQUIA',
    'CARTAGENA': 'BOLÍVAR', 'ENVIGADO': 'ANTIOQUIA',
    'LA ESTRELLA': 'ANTIOQUIA', 'SABANETA': 'ANTIOQUIA',
    'TARAZA': 'ANTIOQUIA', 'TURBO': 'ANTIOQUIA',
}
# No asignar en la semilla estos municipios que comparten nombre o
# requieren contexto en otras regiones de Colombia.
AMBIGUOS_SEMILLA = {'TOLEDO', 'ARGELIA', 'SABANALARGA', 'VENECIA', 'CALDAS'}

DEPARTAMENTOS = [
    'AMAZONAS', 'ANTIOQUIA', 'ARAUCA', 'ATLÁNTICO', 'BOLÍVAR', 'BOYACÁ',
    'CALDAS', 'CAQUETÁ', 'CASANARE', 'CAUCA', 'CESAR', 'CHOCÓ', 'CÓRDOBA',
    'CUNDINAMARCA', 'GUAINÍA', 'GUAVIARE', 'HUILA', 'LA GUAJIRA',
    'MAGDALENA', 'META', 'NARIÑO', 'NORTE DE SANTANDER', 'PUTUMAYO',
    'QUINDÍO', 'RISARALDA', 'SAN ANDRÉS, PROVIDENCIA Y SANTA CATALINA',
    'SANTANDER', 'SUCRE', 'TOLIMA', 'VALLE DEL CAUCA', 'VAUPÉS',
    'VICHADA', 'BOGOTÁ D.C.',
]

BOGOTA_LOCALIDADES = {
    'CHAPINERO', 'USAQUEN', 'SANTA FE', 'SAN CRISTOBAL', 'USME',
    'TUNJUELITO', 'BOSA', 'KENNEDY', 'FONTIBON', 'ENGATIVA', 'SUBA',
    'BARRIOS UNIDOS', 'TEUSAQUILLO', 'LOS MARTIRES', 'ANTONIO NARINO',
    'PUENTE ARANDA', 'LA CANDELARIA', 'RAFAEL URIBE URIBE',
    'CIUDAD BOLIVAR', 'SUMAPAZ',
}

PALABRAS_GENERICAS = {
    'HOSPITAL', 'HOSPITALES', 'CLINICA', 'SALUD', 'IPS', 'EPS', 'ESE',
    'MEDICA', 'MEDICO', 'MEDICARTE', 'FARMACEUTICA', 'FARMACEUTICOS',
    'FARMACIA', 'SERVICIO', 'SERVICIOS', 'DISPENSARIO', 'DISPENSACION',
    'ENTIDAD', 'PROMOTORA', 'CENTRAL', 'SOCIAL', 'EMPRESA', 'EMPRESAS',
    'FUNDACION', 'CORPORACION', 'SAS', 'BIC', 'COLOMBIA', 'SEDE',
    'DOMICILIOS', 'GENERAL', 'PUBLICO', 'PUBLICA', 'MUNICIPAL', 'GRUPO',
    'CONTRATO', 'NUTRICION', 'ALTA', 'COMPLEJIDAD', 'TRANSACCIONAL',
    'PISO', 'REGION', 'ESTADO', 'PARA', 'DEL', 'LAS', 'LOS', 'UNA', 'UNO',
    'SAN', 'SANTA', 'POR', 'CON', 'CONCEJO', 'REORGANIZACION',
}


def txt(valor) -> str:
    if valor is None or pd.isna(valor):
        return ''
    return str(valor).replace('\xa0', ' ').strip()


def norm(valor) -> str:
    s = unicodedata.normalize('NFKD', txt(valor).upper())
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[^A-Z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def contiene(s: str, fragmento: str) -> bool:
    """Coincidencias por palabras completas; evita PEQUE dentro de PEQUEÑO."""
    return bool(re.search(r'(?<![A-Z0-9])' + re.escape(fragmento) +
                          r'(?![A-Z0-9])', s))


def canonical_departamento(depto: str) -> str:
    n = norm(depto)
    if n in {'BOGOTA', 'BOGOTA DC', 'BOGOTA D C', 'BOGOTA DISTRITO CAPITAL',
             'SANTAFE DE BOGOTA', 'SANTA FE DE BOGOTA'}:
        return 'BOGOTÁ D.C.'
    if n in {'SAN ANDRES', 'SAN ANDRES Y PROVIDENCIA',
             'ARCHIPIELAGO DE SAN ANDRES PROVIDENCIA Y SANTA CATALINA'}:
        return 'SAN ANDRÉS, PROVIDENCIA Y SANTA CATALINA'
    return next((d for d in DEPARTAMENTOS if norm(d) == n), '')


@dataclass(frozen=True)
class Coincidencia:
    municipio: str
    departamentos: frozenset[str]
    inicio: int
    fin: int


class Catalogo:
    def __init__(self, registros: list[tuple[str, str]], *, completo: bool):
        byname: dict[str, set[str]] = defaultdict(set)
        for lugar, dpto in registros:
            lugar_normalizado = norm(lugar)
            departamento = canonical_departamento(dpto)
            if lugar_normalizado and departamento:
                byname[lugar_normalizado].add(departamento)
        # Cartagena de Indias es la denominación formal del municipio.
        if 'CARTAGENA DE INDIAS' in byname:
            byname['CARTAGENA'].update(byname['CARTAGENA DE INDIAS'])
        # Variante de Bogotá (en algunos catálogos solo se presenta como BOGOTÁ D.C.).
        byname['BOGOTA'] = {'CUNDINAMARCA'}
        if not completo:
            for nombre in AMBIGUOS_SEMILLA:
                byname.pop(nombre, None)
        self.por_nombre = dict(byname)
        self.completo = completo
        self.nombres_ordenados = sorted(byname, key=lambda s: (-len(s), s))

    def buscar_exacto(self, lugar: str) -> set[str]:
        return self.por_nombre.get(norm(lugar), set())

    def menciones(self, texto: str) -> list[Coincidencia]:
        """Extrae los nombres geográficos más largos, sin solapamientos."""
        s = norm(texto)
        candidatos = []
        usados = []
        for municipio in self.nombres_ordenados:
            if len(municipio) < 4:
                continue
            for encontrado in re.finditer(r'(?<![A-Z0-9])' + re.escape(municipio) +
                                        r'(?![A-Z0-9])', s):
                a, b = encontrado.span()
                if any(a < d and b > c for c, d in usados):
                    continue
                usados.append((a, b))
                candidatos.append(Coincidencia(municipio,
                                    frozenset(self.por_nombre[municipio]), a, b))
        return sorted(candidatos, key=lambda c: c.inicio)


def descargar_divipola(destino: Path) -> list[tuple[str, str]]:
    params = urlencode({'$limit': '5000', '$select':
                         'cod_dpto,dpto,cod_mpio,nom_mpio,tipo_municipio'})
    url = DANE_ENDPOINT + '?' + params
    req = Request(url, headers={'User-Agent': 'LocalizadorMunicipiosColombia/2.0',
                                'Accept': 'application/json'})
    with urlopen(req, timeout=22) as respuesta:
        datos = json.load(respuesta)
    if not isinstance(datos, list) or len(datos) < 1000:
        raise ValueError('Respuesta DIVIPOLA incompleta: menos de 1000 ubicaciones.')
    registros = [
        (str(f.get('nom_mpio', '')), str(f.get('dpto', '')))
        for f in datos if isinstance(f, dict)
    ]
    registros = [(mun, dep) for mun, dep in registros if mun.strip() and dep.strip()]
    if len(registros) < 1000:
        raise ValueError('DIVIPOLA no proporcionó suficientes municipios válidos.')
    temporal = destino.with_suffix('.tmp')
    temporal.write_text(json.dumps(registros, ensure_ascii=False, indent=1),
                        encoding='utf-8')
    temporal.replace(destino)
    return registros


def cargar_catalogo(ruta_script: Path) -> Catalogo:
    archivo = ruta_script.with_name('Catalogo_DIVIPOLA_DANE.json')
    registros = None
    if archivo.exists():
        try:
            registros = json.loads(archivo.read_text(encoding='utf-8'))
            if len(registros) < 1000:
                registros = None
        except (ValueError, OSError):
            registros = None
    necesita_actualizar = (
        not archivo.exists() or
        datetime.now(timezone.utc) - datetime.fromtimestamp(
            archivo.stat().st_mtime, tz=timezone.utc
        ) > timedelta(days=CACHE_DIAS)
    )
    if necesita_actualizar:
        try:
            nuevo = descargar_divipola(archivo)
            registros = nuevo
            print(f'Catálogo DANE actualizado: {len(nuevo)} ubicaciones.')
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as e:
            print(f'No fue posible actualizar DIVIPOLA: {type(e).__name__}: {e}')
    if registros and len(registros) >= 1000:
        print(f'Catálogo oficial disponible: {len(registros)} ubicaciones.')
        return Catalogo(registros, completo=True)
    print('AVISO: catálogo DANE no disponible. Se usará la semilla PARCIAL '
          'de ubicaciones conocidas y después Google Maps.')
    registros = [(k, v) for k, v in SEMILLA.items() if k not in AMBIGUOS_SEMILLA]
    return Catalogo(registros, completo=False)


def descomponer_nombre(producto: str) -> str:
    s = txt(producto).replace('_', ' ').replace('|', ' ')
    # Quita IDs internos solamente al principio.
    s = re.sub(r'^\s*(?:P[ -]?)?M?\d{1,8}\s*[- ]*', '', s, flags=re.I)
    s = re.sub(r'(?<!\w)MDT(?!\w)', ' ', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s)
    return s.strip(' -.,')


def municipio_en_contexto(nombre: str, coincidencia: Coincidencia) -> bool:
    """Controla falsos positivos de marcas coincidentes con municipios.

    'FARMACEUTICA NILO SAS' NO demuestra que esté en Nilo.
    'YOLOMBO ESE HOSPITAL' / 'HOSPITAL ... COCORNA' sí ubican hospital.
    """
    n = norm(descomponer_nombre(nombre))
    municipio = coincidencia.municipio
    if not contiene(n, municipio):
        return False
    if norm(nombre) == municipio:
        return True
    # Identificador con ciudad en el código (p.ej. 6_MDT_BOGOTA_NORTE).
    bruto = txt(nombre).upper()
    if re.search(r'(?:^|_)MDT(?:_|\b)', bruto):
        return True
    if re.match(r'^\s*M?\d{1,8}[_\s-]', bruto):
        return True
    # Ciudad explícitamente asociada a establecimiento (con/de/en).
    if re.search(r'\b(?:DE|DEL|EN|CIUDAD|MUNICIPIO|SEDE)\s+' +
                 re.escape(municipio) + r'\b', n):
        return True
    # Ciudad al principio, seguida de institución, ej. NECHI ESE HOSPITAL.
    if n.startswith(municipio + ' ') and re.search(
        r'\b(?:HOSPITAL|ESE|IPS|EPS|CLINICA|SERVICIO)\b', n[len(municipio):]
    ):
        return True
    # Referencia explícita a región sanitaria o sede: 'REGION DE SALUD SOACHA'.
    if re.search(r'\b(?:REGION DE SALUD|SEDE|MUNICIPIO DE)\s+' +
                 re.escape(municipio) + r'\b', n):
        return True
    # Ciudad al final de un nombre de hospital: "HOSPITAL SAN JUAN ... COCORNA".
    if n.endswith(' ' + municipio) and 'HOSPITAL' in n.split():
        return True
    # Ciudad separada por guion como etiqueta de ubicación.
    if re.search(r'\b' + re.escape(municipio) + r'\s*-\s*',
                 txt(nombre).upper()) or re.search(
                 r'-\s*' + re.escape(municipio) + r'\b', txt(nombre).upper()):
        return True
    return False


def resolver_por_catalogo(territorio: str, producto: str,
                          catalogo: Catalogo) -> tuple[str, str]:
    """Devuelve (departamento, motivo) o ('', motivo) para buscar en Maps."""
    territorio = txt(territorio)
    producto = txt(producto)
    if territorio:
        cands = catalogo.buscar_exacto(territorio)
        if len(cands) == 1:
            return next(iter(cands)), 'Territorio exacto en catálogo'
        # Un departamento indicado como territorio también se acepta directamente.
        depto_explicito = canonical_departamento(territorio)
        if depto_explicito:
            return depto_explicito, 'Departamento escrito directamente'
        return '', 'Territorio no hallado o municipio homónimo'
    if not producto:
        return '', 'Ambas columnas vacías'

    n = norm(descomponer_nombre(producto))
    es_mdt = bool(re.search(r'\bMDT\b', norm(producto)))
    # Chapinero, en particular, es localidad de Bogotá, no municipio DANE.
    if es_mdt:
        for localidad in sorted(BOGOTA_LOCALIDADES, key=len, reverse=True):
            # 'Ciudad Bolívar' y 'San Cristóbal' también designan municipios
            # de otros departamentos: NO asumir Bogotá sin contexto adicional.
            if localidad in catalogo.por_nombre and localidad != 'CHAPINERO':
                continue
            if contiene(n, localidad):
                return 'BOGOTÁ D.C.', f'Localidad de Bogotá: {localidad}'

    # La ciudad y su departamento escritos JUNTOS sirven de evidencia
    # incluso en denominaciones empresariales (MEDELLIN ANTIOQUIA EPS).
    deptos_explicitos = {d for d in DEPARTAMENTOS if contiene(n, norm(d))}
    hallazgos = [
        c for c in catalogo.menciones(n)
        if municipio_en_contexto(producto, c) or
        (len(c.departamentos) == 1 and
         bool(set(c.departamentos) & deptos_explicitos))
    ]
    # Si hay múltiples lugares, solamente aceptar cuando todos apuntan al mismo
    # departamento; no escoger arbitrariamente el primero.
    grupos = [set(c.departamentos) for c in hallazgos]
    if grupos:
        posibles = set.intersection(*grupos)
        if len(posibles) == 1:
            departamento = next(iter(posibles))
            return departamento, 'Municipio(s) inequívoco(s): ' + ', '.join(
                c.municipio for c in hallazgos
            )
        # Departamentos explícitos ayudan solo a DESAMBIGUAR municipios.
        declarados = {d for d in DEPARTAMENTOS if contiene(n, norm(d))}
        # 'SANTANDER' dentro de 'NORTE DE SANTANDER' NO es evidencia separada.
        if 'NORTE DE SANTANDER' in declarados:
            declarados.discard('SANTANDER')
        if 'VALLE DEL CAUCA' in declarados:
            declarados.discard('CAUCA')
        if len(posibles & declarados) == 1:
            return next(iter(posibles & declarados)), 'Municipio desambiguado por departamento'
        return '', 'Municipios homónimos o localidades contradictorias'
    return '', 'No hay municipio extraíble con evidencia de ubicación'


def extraer_departamento_maps(direccion: str) -> str:
    """Detecta un departamento como segmento de dirección, no en el nombre de calle."""
    s = norm(direccion)
    if not s:
        return ''
    # Descarta resultados evidentemente fuera de Colombia.
    paises = ('VENEZUELA', 'MEXICO', 'ARGENTINA', 'PERU', 'ECUADOR', 'PANAMA',
              'CHILE', 'BRASIL', 'ESTADOS UNIDOS', 'BOLIVIA')
    if 'COLOMBIA' not in s and any(contiene(s, pais) for pais in paises):
        return ''
    partes = [norm(x) for x in re.split(r'[,;\n|]', direccion)]
    for parte in reversed(partes):
        if not parte or parte == 'COLOMBIA':
            continue
        if parte.startswith('DEPARTAMENTO DE '):
            parte = parte.replace('DEPARTAMENTO DE ', '', 1)
        # Bogotá puede figurar como 'Bogotá D.C.' o 'Bogotá'.
        dpto = canonical_departamento(parte)
        if dpto:
            return dpto
        # Fichas de poblaciones muestran "Municipio en Antioquia, Colombia".
        m = re.search(r'\b(?:EN|DE|DEL) (.+?)(?: COLOMBIA)?$', parte)
        if m:
            departamento = canonical_departamento(m.group(1))
            if departamento:
                return departamento
    return ''


class CaptchaDetectado(Exception):
    pass


def importar_selenium():
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    return webdriver, TimeoutException, WebDriverException, Options, By, WebDriverWait


def crear_driver():
    webdriver, _, _, Options, _, _ = importar_selenium()
    options = Options()
    if HEADLESS:
        options.add_argument('--headless=new')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--lang=es-CO')
    return webdriver.Chrome(options=options)


def detectar_captcha(driver, By):
    try:
        s = driver.find_element(By.TAG_NAME, 'body').text.lower()
        return any(t in s for t in (
            'unusual traffic', 'tráfico inusual', 'captcha',
            'nuestros sistemas han detectado', 'not a robot'))
    except Exception:
        return False


def consultar_maps(driver, consulta: str) -> tuple[str, list[str]]:
    _, TimeoutException, _, _, By, WebDriverWait = importar_selenium()
    url = 'https://www.google.com/maps/search/?api=1&query=' + quote_plus(consulta)
    driver.get(url)
    try:
        WebDriverWait(driver, 12).until(
            lambda d: d.find_elements(By.TAG_NAME, 'body'))
    except TimeoutException:
        return '', []
    if detectar_captcha(driver, By):
        raise CaptchaDetectado('Google Maps solicita verificación manual.')

    for t in ('Aceptar todo', 'Accept all'):
        try:
            for btn in driver.find_elements(By.XPATH, f"//button[contains(., '{t}')]"):
                if btn.is_displayed():
                    btn.click()
                    break
        except Exception:
            pass

    def titulo():
        for selector in ('h1.DUwDvf', 'h1[class*="fontHeadlineLarge"]'):
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed() and el.text.strip():
                    return el.text.strip()
        return ''

    try:
        WebDriverWait(driver, 6).until(lambda d: bool(titulo()) or
                bool(d.find_elements(By.CSS_SELECTOR, 'a.hfpxzc')))
    except TimeoutException:
        pass
    if not titulo():
        for sel in ('a.hfpxzc', 'div[role="feed"] a[href*="/maps/place/"]'):
            elementos = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elementos:
                if el.is_displayed():
                    try:
                        el.click()
                        WebDriverWait(driver, 6).until(lambda d: bool(titulo()))
                        break
                    except Exception:
                        continue
            if titulo():
                break
    if detectar_captcha(driver, By):
        raise CaptchaDetectado('Google Maps solicita verificación manual.')
    nombre = titulo()
    direcciones = []
    if not nombre:
        return '', []

    # Para municipios puede faltar el botón dirección; se revisan subtítulos.
    for sel in ('button[data-item-id="address"]', '[data-item-id="address"]',
                'button[aria-label^="Dirección:"]',
                'button[aria-label^="Address:"]',
                'div.DkEaL'):
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    s = (el.get_attribute('aria-label') or el.text or '').strip()
                    s = re.sub(r'^(?:Dirección|Address):\s*', '', s, flags=re.I)
                    if s and s not in direcciones:
                        direcciones.append(s)
        except Exception:
            continue
    return nombre, direcciones


def consultas_para_maps(territorio: str, producto: str,
                         catalogo: Catalogo) -> list[tuple[str, str]]:
    """Consultas contextualizadas; no busca municipios ambiguos en aislamiento."""
    if txt(territorio):
        return [(f'{txt(territorio)}, Colombia', 'territorio')]
    limpio = descomponer_nombre(producto)
    if not limpio:
        return []
    candidatos = [(limpio + ', Colombia', 'empresa')]
    n = norm(limpio)
    menciones = catalogo.menciones(n)
    for c in menciones:
        if municipio_en_contexto(producto, c):
            # Si era ambiguo, la descripción de la empresa ayuda a precisar.
            candidatos.append((f'{limpio} {c.municipio}, Colombia', 'empresa'))
            if len(c.departamentos) == 1:
                candidatos.append((f'{c.municipio}, Colombia', 'municipio'))
    # Por nombre exacto de clínica u hospital sin IDs: algunas fichas están
    # indexadas sin razón social adicional; ya las cubre la primera consulta.
    unico = []
    vistos = set()
    for consulta, tipo in candidatos:
        clave = norm(consulta)
        if clave not in vistos:
            unico.append((consulta, tipo))
            vistos.add(clave)
        if len(unico) >= MAX_INTENTOS_MAPS:
            break
    return unico


def validar_maps(nombre_busqueda: str, nombre_mapa: str,
                 direccion: str, tipo: str, catalogo: Catalogo) -> str:
    depto = extraer_departamento_maps(direccion)
    if not depto:
        return ''
    nom = norm(nombre_mapa)
    if tipo == 'territorio':
        return depto if contiene(nom, norm(nombre_busqueda)) or contiene(
            norm(direccion), norm(nombre_busqueda)) else ''
    if tipo == 'municipio':
        return depto if contiene(nom, norm(nombre_busqueda)) else ''
    tokens = {t for t in norm(descomponer_nombre(nombre_busqueda)).split()
              if len(t) >= 4 and t not in PALABRAS_GENERICAS}
    tokens_resultado = set(nom.split())
    # Nombre único o establecimiento con ≥2 tokens distintivos coincidentes.
    coincidentes = tokens & tokens_resultado
    minimo = 2 if len(tokens) >= 4 else 1
    if len(coincidentes) < minimo:
        return ''
    # Evita que el nombre de la empresa supla evidencia geográfica.
    return depto


def resolver_maps(driver, territorio: str, producto: str,
                  catalogo: Catalogo, cache: dict) -> tuple[str, str]:
    for consulta, tipo in consultas_para_maps(territorio, producto, catalogo):
        print(f'    Maps [{tipo}]: {consulta}')
        if consulta not in cache:
            cache[consulta] = consultar_maps(driver, consulta)
            time.sleep(PAUSA_MAPS)
        nombre_mapa, direcciones = cache[consulta]
        clave = territorio if territorio else producto
        for direccion in direcciones:
            dpto = validar_maps(clave, nombre_mapa, direccion, tipo, catalogo)
            if dpto:
                return dpto, f'Maps: {nombre_mapa} | {direccion}'
    return '', 'Maps no pudo confirmar un departamento'


def seleccionar_archivo() -> Path:
    if sys.platform == 'win32':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    from tkinter import Tk, filedialog
    ventana = Tk()
    ventana.withdraw()
    ventana.attributes('-topmost', True)
    try:
        seleccionado = filedialog.askopenfilename(
            title='Selecciona las ubicaciones de Colombia',
            filetypes=[('Excel', '*.xlsx'), ('Todos los archivos', '*.*')],
        )
    finally:
        ventana.destroy()
    if not seleccionado:
        raise SystemExit('No se seleccionó archivo.')
    return Path(seleccionado)


def guardar(filas: list[dict], salida: Path):
    """Guarda ambos grupos en un solo libro, sin cambiar datos ni estatus.

    La clasificación depende únicamente de cuál columna original contiene
    texto. Si una fila tuviera ambas columnas informadas, aparece en las dos
    hojas para evitar perder información.
    """
    datos = pd.DataFrame(filas, columns=COLUMNAS).fillna('')
    territorios = datos.loc[
        datos[COL_TERRITORIO].astype(str).str.strip().ne(''),
        [COL_TERRITORIO, COL_DEPTO, COL_ESTATUS],
    ]
    productos = datos.loc[
        datos[COL_PRODUCTO].astype(str).str.strip().ne(''),
        [COL_PRODUCTO, COL_DEPTO, COL_ESTATUS],
    ]

    with pd.ExcelWriter(salida, engine='openpyxl') as writer:
        territorios.to_excel(
            writer, sheet_name=COL_TERRITORIO, index=False,
            freeze_panes=(1, 0),
        )
        productos.to_excel(
            writer, sheet_name=COL_PRODUCTO, index=False,
            freeze_panes=(1, 0),
        )


def ejecutar(archivo: Path, solo_catalogo: bool):
    if not archivo.exists():
        raise FileNotFoundError(archivo)
    df = pd.read_excel(archivo, dtype=str, keep_default_na=False, engine='openpyxl')
    if list(df.columns[:2]) != [COL_TERRITORIO, COL_PRODUCTO]:
        raise ValueError('El archivo debe comenzar con las columnas '
                         f'{COL_TERRITORIO}, {COL_PRODUCTO}.')
    # Permite usar tanto Entrada.xlsx como la salida anterior de cuatro columnas.
    filas = [{COL_TERRITORIO: txt(row[COL_TERRITORIO]),
              COL_PRODUCTO: txt(row[COL_PRODUCTO]),
              COL_DEPTO: '', COL_ESTATUS: 'PENDIENTE'}
             for _, row in df.iterrows()]
    catalogo = cargar_catalogo(Path(__file__).resolve())
    salida = archivo.with_name(archivo.stem + '_v3.xlsx' if
        COL_DEPTO in df.columns else archivo.stem + '_DepartamentosColombia_v3.xlsx')
    print(f'Procesando {len(filas)} filas | modo: '
          f'{"solo catálogo" if solo_catalogo else "catálogo + Maps"}')
    driver = None
    cache = {}
    try:
        for i, row in enumerate(filas, 1):
            territorio, producto = row[COL_TERRITORIO], row[COL_PRODUCTO]
            dpto, evidencia = resolver_por_catalogo(territorio, producto, catalogo)
            if not dpto and not solo_catalogo and (territorio or producto):
                if driver is None:
                    try:
                        driver = crear_driver()
                    except Exception as e:
                        print('No fue posible abrir Chrome:', e)
                        print('Se continúa solo con catálogo en este lote.')
                        solo_catalogo = True
                if driver is not None:
                    try:
                        dpto, evidencia = resolver_maps(
                            driver, territorio, producto, catalogo, cache)
                    except CaptchaDetectado as e:
                        print('CAPTCHA: ' + str(e) + ' Se conserva el avance.')
                        break
                    except Exception as e:
                        evidencia = f'Error de Maps: {type(e).__name__}: {e}'
            row[COL_DEPTO] = dpto
            row[COL_ESTATUS] = 'ENCONTRADO' if dpto else 'NO ENCONTRADO'
            print(f'[{i:02d}/{len(filas)}] '
                  f'{territorio or producto} -> {dpto or "SIN CONFIRMAR"} '
                  f'({evidencia})')
            if i % GUARDAR_CADA == 0:
                guardar(filas, salida)
    except KeyboardInterrupt:
        print('Interrumpido: conservando resultados procesados.')
    finally:
        guardar(filas, salida)
        if driver is not None:
            driver.quit()
    conteo = pd.DataFrame(filas)[COL_ESTATUS].value_counts().to_dict()
    print('Resumen:', conteo)
    print('Excel generado:', salida)
    return salida


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('excel', nargs='?', type=Path,
                   help='Ruta del Excel, o selector gráfico si se omite')
    p.add_argument('--solo-catalogo', action='store_true',
                   help='No abre Chrome. Solo utiliza coincidencias inequívocas.')
    args = p.parse_args()
    ejecutar(args.excel or seleccionar_archivo(), args.solo_catalogo)


if __name__ == '__main__':
    main()
