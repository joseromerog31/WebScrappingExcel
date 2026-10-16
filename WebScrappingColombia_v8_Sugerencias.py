from __future__ import annotations

import argparse
import ctypes
from difflib import SequenceMatcher
from difflib import SequenceMatcher
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, unquote

import pandas as pd

COL_TERRITORIO = 'TERRITORIO FINAL'
COL_PRODUCTO = 'NOMBRE UBICACION PRODUCTO'
COL_DEPTO = 'DEPARTAMENTO ENCONTRADO'
COL_ESTATUS = 'ESTATUS'
COL_APROX_LUGAR = 'LUGAR APROXIMADO'
COL_APROX_DEPTO = 'DEPARTAMENTO APROXIMADO'
COLUMNAS = [COL_TERRITORIO, COL_PRODUCTO, COL_DEPTO, COL_ESTATUS,
            COL_APROX_LUGAR, COL_APROX_DEPTO]

HEADLESS = True
PAUSA_MAPS = 1.3
GUARDAR_CADA = 10
MAX_CONSULTAS_TERRITORIO = 4
MAX_CONSULTAS_PRODUCTO = 7  # Más variantes para nombres comerciales abreviados

# No es un catálogo de ciudades: sirve para reconocer SOLO el departamento que Google Maps devuelve en su dirección, categoría o URL.
DEPARTAMENTOS = (
    'AMAZONAS', 'ANTIOQUIA', 'ARAUCA', 'ATLÁNTICO', 'BOLÍVAR', 'BOYACÁ',
    'CALDAS', 'CAQUETÁ', 'CASANARE', 'CAUCA', 'CESAR', 'CHOCÓ', 'CÓRDOBA',
    'CUNDINAMARCA', 'GUAINÍA', 'GUAVIARE', 'HUILA', 'LA GUAJIRA',
    'MAGDALENA', 'META', 'NARIÑO', 'NORTE DE SANTANDER', 'PUTUMAYO',
    'QUINDÍO', 'RISARALDA', 'SAN ANDRÉS, PROVIDENCIA Y SANTA CATALINA',
    'SANTANDER', 'SUCRE', 'TOLIMA', 'VALLE DEL CAUCA', 'VAUPÉS', 'VICHADA',
)

DEPTOS_ALIASES = {
    'SAN ANDRES': 'SAN ANDRÉS, PROVIDENCIA Y SANTA CATALINA',
    'SAN ANDRES Y PROVIDENCIA': 'SAN ANDRÉS, PROVIDENCIA Y SANTA CATALINA',
    'SAN ANDRES PROVIDENCIA Y SANTA CATALINA': 'SAN ANDRÉS, PROVIDENCIA Y SANTA CATALINA',
    'ARCHIPIELAGO DE SAN ANDRES PROVIDENCIA Y SANTA CATALINA': 'SAN ANDRÉS, PROVIDENCIA Y SANTA CATALINA',
    'VALLE': 'VALLE DEL CAUCA',
    'BOGOTA': 'CUNDINAMARCA',
    'BOGOTA DC': 'CUNDINAMARCA',
    'BOGOTA D C': 'CUNDINAMARCA',
    'BOGOTA DISTRITO CAPITAL': 'CUNDINAMARCA',
    'DISTRITO CAPITAL': 'CUNDINAMARCA',
    'SANTA FE DE BOGOTA': 'CUNDINAMARCA',
    'SANTAFE DE BOGOTA': 'CUNDINAMARCA',
}

PALABRAS_OPERATIVAS = {
    'MDT', 'DOMICILIOS', 'DOMICILIO', 'PISO', 'NORTE', 'SUR', 'OCCIDENTE',
    'ORIENTE', 'ESTE', 'OESTE', 'REGIONAL', 'REGION', 'ZONA', 'SEDE',
    'MEDICARTE', 'NUTRICION', 'TRANSACCIONAL', 'POLICLINICA',
    'DISPENSACION', 'CLINICA', 'HOSPITAL',
}

PALABRAS_GENERICAS = {
    'HOSPITAL', 'HOSPITALES', 'CLINICA', 'CLINICAS', 'SALUD', 'IPS', 'EPS',
    'ESE', 'MEDICA', 'MEDICO', 'MEDICARTE', 'FARMACEUTICA', 'FARMACIA',
    'SERVICIO', 'SERVICIOS', 'ENTIDAD', 'PROMOTORA', 'CENTRAL', 'SOCIAL',
    'EMPRESA', 'EMPRESAS', 'FUNDACION', 'CORPORACION', 'SAS', 'S A S',
    'COLOMBIA', 'GENERAL', 'PUBLICO', 'PUBLICA', 'MUNICIPAL', 'CENTRO',
    'CONTRATO', 'PISO', 'REGIONAL', 'DOMICILIOS', 'SEDE',
}

CATEGORIAS_GEOGRAFICAS = (
    'MUNICIPIO', 'CIUDAD EN', 'PUEBLO EN', 'LOCALIDAD EN', 'DISTRITO EN',
    'TOWN IN', 'CITY IN', 'MUNICIPALITY', 'VILLAGE IN', 'DEPARTAMENTO',
    'REGION ADMINISTRATIVA',
)

PAISES_AJENOS = (
    'MEXICO', 'VENEZUELA', 'ECUADOR', 'PERU', 'ARGENTINA', 'PANAMA',
    'BRASIL', 'CHILE', 'ESTADOS UNIDOS', 'BOLIVIA', 'COSTA RICA', 'ESPANA',
)


def txt(valor) -> str:
    if valor is None or pd.isna(valor):
        return ''
    return str(valor).replace('\xa0', ' ').strip()


def norm(valor) -> str:
    # Quitar tildes de las vocales, pero conservar la Ñ (distinta de N).
    s = txt(valor).upper()
    s = s.replace('Ñ', '\uE000')  # Proteger Ñ de la descomposición Unicode.
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace('\uE000', 'Ñ')
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9Ñ]+', ' ', s)).strip()


def palabra_entera(texto: str, termino: str) -> bool:
    return bool(re.search(r'(?<![A-Z0-9Ñ])' + re.escape(norm(termino)) +
                          r'(?![A-Z0-9Ñ])', norm(texto)))


DEPTOS_POR_NOMBRE = {norm(d): d for d in DEPARTAMENTOS}
DEPTOS_POR_NOMBRE.update(DEPTOS_ALIASES)


def canonico(valor: str) -> str:
    # Solo convierte un texto EXACTO en el nombre de un departamento.
    s = norm(valor)
    for prefijo in ('DEPARTAMENTO DEL ', 'DEPARTAMENTO DE ',
                    'DEPARTAMENTO ', 'DPTO DE ', 'DPTO ', 'DEPT OF '):
        if s.startswith(prefijo):
            s = s[len(prefijo):]
    return DEPTOS_POR_NOMBRE.get(s, '')


def descomponer_nombre(producto: str) -> str:
    s = txt(producto).replace('_', ' ').replace('|', ' ')
    s = re.sub(r'^\s*(?:P[ -]?)?M?\d{1,8}\s*[- ]*', '', s, flags=re.I)
    s = re.sub(r'(?<!\w)MDT(?!\w)', ' ', s, flags=re.I)
    return re.sub(r'\s+', ' ', s).strip(' -.,')


def extraer_pista(producto: str) -> str:
    # Identifica candidatos
    original = txt(producto)
    # Identificadores del tipo 6_MDT_BOGOTA_NORTE o M8007_MDT_VILLAVICENCIO.
    m = re.search(r'(?:^|_)MDT_(.+)', original, flags=re.I)
    if m:
        partes = [p for p in re.split(r'[_\s-]+', m.group(1)) if p]
        partes = [p for p in partes if norm(p) not in PALABRAS_OPERATIVAS
                  and not p.isdigit()]
        pista = ' '.join(partes).strip()
        if pista:
            return pista

    # Municipio antes de un establecimiento sanitario: "LA CEJA CLINICA...",
    # "SAN JUAN DE URABA E.S.E HOSPITAL...", "NECHI ESE HOSPITAL...".
    n = norm(descomponer_nombre(original))
    m = re.search(r'\b(?:E\s+S\s+E|ESE|HOSPITAL|CLINICA|IPS|EPS)\b', n)
    if m:
        prefijo = n[:m.start()].strip(' -.')
        if prefijo:
            tokens = norm(prefijo).split()
            if (1 <= len(tokens) <= 5 and
                    any(t not in PALABRAS_GENERICAS for t in tokens)):
                return prefijo
    return ''


def consultas_para_maps(territorio: str, producto: str) -> list[tuple[str, str, str]]:
    # (consulta, tipo de validación, nombre objetivo), por prioridad.
    if txt(territorio):
        valor = txt(territorio)  # Sin cambiar el valor original.
        candidatos = [
            (f'{valor}, Colombia', 'territorio', valor),
            (f'municipio de {valor}, Colombia', 'territorio', valor),
            (f'{valor} Colombia municipio', 'territorio', valor),
            (f'{valor}, departamento, Colombia', 'territorio', valor),
        ]
        limite = MAX_CONSULTAS_TERRITORIO
    else:
        limpio = descomponer_nombre(producto)
        if not limpio:
            return []
        pista = extraer_pista(producto)
        # Referencias de sistema, no nombres oficiales: probar el texto sin codigo,
        # luego el nombre comercial y por ultimo variantes de ubicacion.
        codificado = bool(re.search(r'(?:^|_)MDT_', txt(producto), flags=re.I))
        nombre_sin_sociedad = re.sub(
            r'\b(?:S\s*\.?\s*A\s*\.?\s*S\s*\.?|S\s*\.?\s*A\s*\.?|LTDA)\b.*$',
            '', limpio, flags=re.I).strip(' -.')
        # Variante limpia de abreviaturas operativas habituales.
        simplificado = re.sub(r'\b(?:CTRO|TRAT|INV|SERV|INT|D|S F)\b',
                              ' ', nombre_sin_sociedad, flags=re.I)
        simplificado = re.sub(r'[^\w\s]+', ' ', simplificado)
        simplificado = re.sub(r'\s+', ' ', simplificado).strip()
        candidatos = [(f'{limpio}, Colombia', 'empresa', limpio)]
        for variacion in (nombre_sin_sociedad, simplificado):
            if variacion and norm(variacion) != norm(limpio):
                candidatos.append((f'{variacion}, Colombia', 'empresa', variacion))
        if pista and norm(pista) != norm(limpio):
            # Buscar establecimiento asociado a la ubicacion citada.
            candidatos.append((f'{nombre_sin_sociedad}, {pista}, Colombia',
                               'empresa', nombre_sin_sociedad))
        # En 'MEDICARTE_POLICLINICA_BARRANCABERMEJA' el municipio es una
        # pista, pero no asumimos que se haya localizado la clinica.
        ubicaciones_evidentes = ('BOGOTA', 'CALI', 'SOACHA', 'JAMUNDI',
                                'BARRANCABERMEJA', 'APARTADO', 'VILLETA')
        for ubicacion in ubicaciones_evidentes:
            if palabra_entera(limpio, ubicacion):
                candidatos.append((f'{limpio}, {ubicacion}, Colombia',
                                   'empresa', limpio))
                break
        # Para codigos MDT, probar la marca comercial sin el identificador.
        if codificado:
            marca = re.sub(r'\b(?:NORTE|SUR|ORIENTE|OCCIDENTE|ZONA|SEDE)\b',
                           ' ', limpio, flags=re.I)
            marca = re.sub(r'\s+', ' ', marca).strip()
            if marca and norm(marca) != norm(limpio):
                candidatos.append((f'{marca}, Colombia', 'empresa', marca))
        # Nombres alternativos comprobables en Maps; no asignan departamento.
        alias_busqueda = {
            'FUNDACION CTIC': 'CENTRO DE TRATAMIENTO E INVESTIGACION SOBRE CANCER CTIC',
            'ESPECIALIDADES MEDICAS METROPOLITANAS': 'CLINICA EMMSA',
            'INSTITUTO COLOMBIANO D SISTEMA NERVIOSO': 'INSTITUTO COLOMBIANO DEL SISTEMA NERVIOSO',
            'SUBRED INT DE SERV DE SALUD SUROCCIDENTE': 'SUBRED INTEGRADA DE SERVICIOS DE SALUD SUR OCCIDENTE',
            'CENTRO CARDIVASCULAR SOMER IN CARE': 'CENTRO CARDIOVASCULAR SOMER IN CARE',
            'FUNDACION CTIC CTRO TRAT INV S CANCER': 'CENTRO DE TRATAMIENTO E INVESTIGACION SOBRE CANCER CTIC',
        }
        for prefijo, alternativo in alias_busqueda.items():
            if norm(limpio).startswith(prefijo):
                candidatos.insert(1, (f'{alternativo}, Colombia', 'empresa', alternativo))
                break
        limite = MAX_CONSULTAS_PRODUCTO

    resultado = []
    visto = set()
    for consulta, tipo, objetivo in candidatos:
        llave = norm(consulta)
        if llave not in visto:
            resultado.append((consulta, tipo, objetivo))
            visto.add(llave)
        if len(resultado) >= limite:
            break
    return resultado


def _evidencia_extranjera(texto: str) -> bool:
    s = norm(texto)
    return ('COLOMBIA' not in s and any(palabra_entera(s, p) for p in PAISES_AJENOS))


def departamento_en_evidencia(evidencia: str) -> str:
    """Interpreta SOLO contexto administrativo explícito de Maps.

    No se deduce que «Caldas» sea el departamento por figurar en el nombre
    «Caldas, Antioquia, Colombia»; tiene prioridad el componente junto a Colombia.
    """
    texto = unquote(txt(evidencia)).replace('+', ' ')
    if not texto or _evidencia_extranjera(texto):
        return ''
    s = norm(texto)

    # La dirección y el slug de Maps suelen contener: Municipio, Departamento,
    # Colombia. La división en componentes evita confundir lugares homónimos.
    componentes = [norm(x) for x in re.split(r'[,;|\n]', texto) if norm(x)]
    for i, componente in enumerate(componentes):
        if componente == 'COLOMBIA' and i >= 2:
            departamento = canonico(componentes[i - 1])
            if departamento:
                return departamento
        if componente in ('BOGOTA', 'BOGOTA DC', 'BOGOTA D C'):
            if i + 1 < len(componentes) and componentes[i + 1] == 'COLOMBIA':
                return 'CUNDINAMARCA'

    # En fichas de población el subtítulo suele mostrar «Municipio en Huila»
    # sin indicar Colombia. Solo se interpretan patrones administrativos.
    patrones = (
        r'\b(?:MUNICIPIO|CIUDAD|LOCALIDAD|PUEBLO|DISTRITO|POBLACION)\s+'
        r'(?:UBICADO\s+)?(?:EN|DEL|DE)\s+(?:EL\s+)?'
        r'(?:DEPARTAMENTO\s+(?:DE|DEL)\s+)?([A-Z ]+?)(?:\s+COLOMBIA|$)',
        r'\b(?:MUNICIPALITY|CITY|TOWN|VILLAGE|DISTRICT)\s+IN\s+'
        r'(?:THE\s+)?([A-Z ]+?)(?:\s+COLOMBIA|$)',
        r'\bDEPARTAMENTO\s+(?:DE|DEL)\s+([A-Z ]+?)(?:\s+COLOMBIA|$)',
    )
    for patron in patrones:
        m = re.search(patron, s)
        if m:
            d = canonico(m.group(1))
            if d:
                return d

    # Direcciones sin «Colombia»: último componente administrativo.
    # No asumir que «Caldas, Colombia» confirma el departamento Caldas.
    if len(componentes) >= 2 and 'COLOMBIA' not in componentes:
        return canonico(componentes[-1])
    return ''


def lugar_coincide(objetivo: str, nombre: str, tipo: str,
                   evidencias: list[tuple[str, str]]) -> bool:
    objetivo_n = norm(objetivo)
    nombre_n = norm(nombre)
    if not nombre_n or not objetivo_n:
        return False

    if tipo in ('territorio', 'lugar_pista'):
        # A diferencia de la v4, admitir «Municipio de X», «X, Antioquia»
        # y «X - Colombia»; se valida que el nombre empiece en X, nunca
        # basta una coincidencia con una palabra cualquiera.
        prefijos = (objetivo_n, 'MUNICIPIO DE ' + objetivo_n,
                    'MUNICIPIO ' + objetivo_n, 'CIUDAD DE ' + objetivo_n,
                    'DISTRITO DE ' + objetivo_n)
        titulo_correcto = any(nombre_n == p or nombre_n.startswith(p + ' ')
                              for p in prefijos)
        if not titulo_correcto:
            return False
        categorias = [norm(v) for fuente, v in evidencias
                      if fuente in ('categoria', 'subtitulo')]
        categorias_negocio = ('RESTAURANTE', 'HOTEL', 'CAFETERIA', 'HOSPITAL',
                              'FARMACIA', 'TIENDA', 'GIMNASIO', 'BANCO',
                              'EMPRESA', 'CLINICA', 'BAR', 'CENTRO COMERCIAL')
        if any(any(palabra_entera(c, x) for x in categorias_negocio)
               for c in categorias):
            return False
        # El nombre de la ficha y un contexto de departamento son evidencia
        # suficiente. No exigir que la URL repita el municipio: Google Maps
        # puede usar coordenadas o un slug distinto en su URL.
        es_geografica = any(
            any(palabra_entera(c, k) for k in CATEGORIAS_GEOGRAFICAS)
            for c in categorias)
        tiene_direccion_territorial = any(
            fuente in ('direccion', 'subtitulo', 'url') and
            bool(departamento_en_evidencia(valor))
            for fuente, valor in evidencias)
        return es_geografica or tiene_direccion_territorial

    # No validar nombres genericamente cortos como DISPENSACION: elegir el
    # primer resultado de Maps generaria numerosos falsos positivos.
    tokens = {t for t in objetivo_n.split() if len(t) >= 4
              and t not in PALABRAS_GENERICAS}
    nombre_tokens = set(nombre_n.split())
    if not tokens:
        return False
    iguales = tokens & nombre_tokens
    # Mantener aceptacion de nombres empresariales con dos terminos propios.
    if len(iguales) >= (2 if len(tokens) >= 2 else 1):
        return True
    # Un token distintivo largo puede identificar una marca unica en Maps.
    # Se permite solamente si es suficientemente largo y exacto.
    if any(len(t) >= 9 for t in iguales):
        return True
    # Variantes menores de escritura, no palabras inventadas ni coincidencias
    # parciales arbitrarias. Requiere un nombre relativamente largo.
    return (len(objetivo_n) >= 15 and len(nombre_n) >= 12 and
            SequenceMatcher(None, objetivo_n, nombre_n).ratio() >= 0.86)



@dataclass
class FichaMaps:
    nombre: str
    evidencias: list[tuple[str, str]]
    url: str


def extraer_url_ubicacion(url: str) -> str:
    # Extrae únicamente la parte descriptiva /maps/place/... de Maps.
    decoded = unquote(url)
    m = re.search(r'/maps/place/([^/@?!]+)', decoded)
    if m:
        return m.group(1).replace('+', ' ')
    return ''


class CaptchaDetectado(Exception):
    pass


def importar_selenium():
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    return webdriver, TimeoutException, Options, By, WebDriverWait


def crear_driver(headless=True):
    webdriver, _, Options, _, _ = importar_selenium()
    opciones = Options()
    if headless:
        opciones.add_argument('--headless=new')
    opciones.add_argument('--window-size=1920,1080')
    opciones.add_argument('--lang=es-CO')
    opciones.add_argument('--disable-notifications')
    return webdriver.Chrome(options=opciones)


def detectar_captcha(driver, By) -> bool:
    try:
        s = driver.find_element(By.TAG_NAME, 'body').text.lower()
        return any(x in s for x in ('unusual traffic', 'tráfico inusual',
                    'captcha', 'nuestros sistemas han detectado', "i'm not a robot"))
    except Exception:
        return False


def cerrar_consentimiento(driver, By):
    for texto in ('Aceptar todo', 'Accept all'):
        try:
            for btn in driver.find_elements(By.XPATH, f"//button[contains(., '{texto}')]"):
                if btn.is_displayed():
                    btn.click()
                    return
        except Exception:
            pass


def _nombre_ficha(driver, By):
    for sel in ('h1.DUwDvf', 'h1[class*="fontHeadlineLarge"]'):
        try:
            for x in driver.find_elements(By.CSS_SELECTOR, sel):
                if x.is_displayed() and x.text.strip():
                    return x.text.strip()
        except Exception:
            continue
    return ''


def consultar_maps(driver, consulta: str) -> FichaMaps:
    """Extracción de ficha inspirada en WebScrappingLATAM.

    No usa los resultados del buscador general ni DIVIPOLA. Si Google entrega
    varias fichas se abre la primera; el evaluador valida después su identidad.
    """
    _, TimeoutException, _, By, WebDriverWait = importar_selenium()
    url = 'https://www.google.com/maps/search/?api=1&query=' + quote_plus(consulta)
    driver.get(url)
    cerrar_consentimiento(driver, By)
    try:
        WebDriverWait(driver, 14).until(
            lambda d: bool(_nombre_ficha(d, By)) or
            bool(d.find_elements(By.CSS_SELECTOR, 'a.hfpxzc')) or
            bool(d.find_elements(By.CSS_SELECTOR, 'div[role="feed"]')))
    except TimeoutException:
        pass
    if detectar_captcha(driver, By):
        raise CaptchaDetectado('Maps solicitó CAPTCHA/verificación manual.')

    url_resultado = ''
    if not _nombre_ficha(driver, By):
        # Misma lógica LATAM: detectar lista y entrar a la primera ficha.
        limite = time.time() + 8
        while time.time() < limite and not _nombre_ficha(driver, By):
            abierto = False
            for sel in ('a.hfpxzc', 'div[role="feed"] a[href*="/maps/place/"]'):
                try:
                    enlaces = [x for x in driver.find_elements(By.CSS_SELECTOR, sel)
                               if x.is_displayed()]
                    if not enlaces:
                        continue
                    enlace = enlaces[0]
                    url_resultado = enlace.get_attribute('href') or ''
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", enlace)
                    enlace.click()
                    abierto = True
                    try:
                        WebDriverWait(driver, 7).until(
                            lambda d: bool(_nombre_ficha(d, By)))
                    except TimeoutException:
                        pass
                    break
                except Exception:
                    continue
            if abierto:
                break
            time.sleep(0.4)

    if detectar_captcha(driver, By):
        raise CaptchaDetectado('Maps solicitó CAPTCHA/verificación manual.')
    nombre = _nombre_ficha(driver, By)
    if not nombre:
        return FichaMaps('', [], driver.current_url)

    try:
        WebDriverWait(driver, 4).until(lambda d: bool(d.find_elements(
            By.CSS_SELECTOR, '[data-item-id="address"], div.DkEaL')))
    except TimeoutException:
        pass

    evidencias = []
    vistos = set()

    def anadir(fuente, valor):
        valor = txt(valor)
        if valor and (fuente, norm(valor)) not in vistos:
            evidencias.append((fuente, valor))
            vistos.add((fuente, norm(valor)))

    # Dirección: equivalente a get_address() de LATAM.
    for sel in ('button[data-item-id="address"]', '[data-item-id="address"]',
                'button[aria-label^="Dirección:"]',
                'button[aria-label^="Address:"]'):
        try:
            for x in driver.find_elements(By.CSS_SELECTOR, sel):
                if x.is_displayed():
                    dato = x.get_attribute('aria-label') or x.text or ''
                    anadir('direccion', re.sub(
                        r'^(?:Dirección|Address):\s*', '', dato, flags=re.I))
        except Exception:
            pass

    # Capturar por separado categorías y subtítulos; nunca inferir
    # departamentos de reseñas o cualquier texto indiscriminado de la página.
    for sel in ('div.DkEaL', '[class*="fontBodyMedium"]',
                '[class*="fontBodySmall"]'):
        try:
            for x in driver.find_elements(By.CSS_SELECTOR, sel)[:24]:
                if not x.is_displayed():
                    continue
                valor = txt(x.text)
                if not valor or len(valor) > 180:
                    continue
                if sel == 'div.DkEaL':
                    anadir('categoria', valor)
                if (palabra_entera(valor, 'COLOMBIA') or
                    any(palabra_entera(valor, k) for k in
                        ('MUNICIPIO', 'CIUDAD EN', 'DEPARTAMENTO',
                         'CITY IN', 'MUNICIPALITY', 'TOWN IN',
                         'PUEBLO EN', 'LOCALIDAD EN'))):
                    anadir('subtitulo', valor)
        except Exception:
            continue
    for sel in ('[aria-label^="Ubicado en"]', '[aria-label^="Located in"]'):
        try:
            for x in driver.find_elements(By.CSS_SELECTOR, sel)[:6]:
                anadir('subtitulo', x.get_attribute('aria-label'))
        except Exception:
            pass

    # El enlace del resultado puede tener una ruta descriptiva más rica
    # que current_url (LATAM preserva precisamente ese href).
    for enlace in (driver.current_url, url_resultado):
        anadir('url', extraer_url_ubicacion(enlace))
    return FichaMaps(nombre, evidencias, driver.current_url)


def evaluar_ficha(ficha: FichaMaps, tipo: str, objetivo: str) -> tuple[str, str]:
    if not ficha.nombre:
        return '', 'Ficha no encontrada'
    if not lugar_coincide(objetivo, ficha.nombre, tipo, ficha.evidencias):
        return '', f'No coincide el nombre: {ficha.nombre}'

    # Evidencias tienen prioridades: dirección, subtítulo, categoría, URL.
    # Si varias direcciones/subtítulos se contradicen, no elegir al azar.
    prioridades = {'direccion': 0, 'subtitulo': 1, 'categoria': 2, 'url': 3}
    hallazgos = []
    for fuente, evidencia in ficha.evidencias:
        d = departamento_en_evidencia(evidencia)
        if d:
            hallazgos.append((prioridades.get(fuente, 9), d, fuente, evidencia))
    if not hallazgos:
        # Convención acordada para este proyecto: Bogotá = Cundinamarca.
        # Solo se usa cuando Maps confirma una ficha geográfica de Bogotá.
        bogota = {'BOGOTA', 'BOGOTA DC', 'BOGOTA D C', 'BOGOTA DISTRITO CAPITAL'}
        if (tipo in ('territorio', 'lugar_pista') and
                norm(ficha.nombre) in bogota and
                any(fuente == 'url' and palabra_entera(valor, 'BOGOTA')
                    for fuente, valor in ficha.evidencias)):
            return 'CUNDINAMARCA', f'Maps: Bogotá | regla de clasificación'
        return '', f'Ficha sin departamento explícito: {ficha.nombre}'
    mejor = min(x[0] for x in hallazgos)
    favoritos = [x for x in hallazgos if x[0] == mejor]
    departamentos = {x[1] for x in favoritos}
    if len(departamentos) != 1:
        return '', 'Fuentes geográficas contradictorias en Maps'
    seleccionado = favoritos[0]
    return seleccionado[1], f'Maps: {ficha.nombre} | {seleccionado[2]}: {seleccionado[3]}'


def sugerencia_de_ficha(ficha: FichaMaps, objetivo: str, tipo: str):
    """Obtiene una sugerencia de Maps, NUNCA una confirmación.

    Solo sugiere cuando el título se relaciona razonablemente con la consulta
    y Maps contiene evidencia administrativa de un departamento inequívoco.
    """
    if not ficha.nombre:
        return None
    nombre, buscado = norm(ficha.nombre), norm(objetivo)
    if not nombre or not buscado:
        return None

    # Evitar candidatos genéricos y coincidencias de una sola palabra común.
    ignorar = {'COLOMBIA', 'DE', 'DEL', 'LA', 'EL', 'LOS', 'LAS', 'EN',
               'SAS', 'IPS', 'ESE', 'EMPRESA', 'CENTRO', 'SERVICIOS',
               'SALUD', 'HOSPITAL', 'CLINICA', 'MUNICIPIO', 'CIUDAD'}
    a = {x for x in buscado.split() if len(x) >= 3 and x not in ignorar}
    b = {x for x in nombre.split() if len(x) >= 3 and x not in ignorar}
    similitud = SequenceMatcher(None, buscado, nombre).ratio()
    coincidencia = len(a & b) / max(1, len(a))
    puntuacion = max(similitud, coincidencia)
    if (not a or not b or (similitud < 0.48 and coincidencia < 0.5)):
        return None

    # Para municipios, no proponer un comercio con nombre similar.
    if tipo in ('territorio', 'lugar_pista'):
        categorias = [norm(v) for fuente, v in ficha.evidencias
                      if fuente == 'categoria']
        comerciales = ('RESTAURANTE', 'HOTEL', 'CAFETERIA', 'HOSPITAL',
                       'CLINICA', 'FARMACIA', 'TIENDA', 'BANCO', 'BAR')
        if any(any(palabra_entera(c, x) for x in comerciales)
               for c in categorias):
            return None

    prioridades = {'direccion': 0, 'subtitulo': 1, 'categoria': 2, 'url': 3}
    hallazgos = [(prioridades.get(fuente, 9), departamento_en_evidencia(valor))
                 for fuente, valor in ficha.evidencias]
    hallazgos = [(nivel, depto) for nivel, depto in hallazgos if depto]
    if not hallazgos:
        return None
    mejor_nivel = min(nivel for nivel, _ in hallazgos)
    departamentos = {depto for nivel, depto in hallazgos if nivel == mejor_nivel}
    if len(departamentos) != 1:
        return None
    departamento = departamentos.pop()
    return puntuacion, ficha.nombre, norm(departamento)


def resolver_maps(driver, territorio: str, producto: str,
                  cache: dict[str, FichaMaps]) -> tuple[str, str, str, str]:
    ultimo_motivo = 'No hay términos para consultar'
    aproximado = None
    for consulta, tipo, objetivo in consultas_para_maps(territorio, producto):
        print(f'    Maps [{tipo}]: {consulta}')
        if consulta not in cache:
            cache[consulta] = consultar_maps(driver, consulta)
            time.sleep(PAUSA_MAPS)
        ficha = cache[consulta]
        departamento, motivo = evaluar_ficha(ficha, tipo, objetivo)
        if departamento:
            # Hallazgo confirmado: las columnas aproximadas permanecen vacías.
            return departamento, motivo, '', ''
        sugerencia = sugerencia_de_ficha(ficha, objetivo, tipo)
        if sugerencia and (aproximado is None or sugerencia[0] > aproximado[0]):
            aproximado = sugerencia
        ultimo_motivo = motivo
        print(f'      Sin confirmar: {motivo}')
    if aproximado:
        _, lugar, depto = aproximado
        return '', ultimo_motivo, lugar, depto
    return '', ultimo_motivo, '', ''


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
        elegido = filedialog.askopenfilename(
            title='Selecciona el Excel de ubicaciones de Colombia',
            filetypes=[('Excel', '*.xlsx'), ('Todos los archivos', '*.*')])
    finally:
        ventana.destroy()
    if not elegido:
        raise SystemExit('No se seleccionó un archivo.')
    return Path(elegido)


def guardar(filas: list[dict], ruta: Path):
    # Dos hojas, cinco columnas cada una.
    df = pd.DataFrame(filas, columns=COLUMNAS).fillna('')
    # El departamento exportado siempre va en MAYUSCULAS y SIN TILDES, conservando la Ñ.
    for columna in (COL_DEPTO, COL_APROX_DEPTO):
        df[columna] = df[columna].map(lambda valor: norm(valor) if txt(valor) else '')
    with pd.ExcelWriter(ruta, engine='openpyxl') as writer:
        for nombre in (COL_TERRITORIO, COL_PRODUCTO):
            hoja = df.loc[df[nombre].astype(str).str.strip().ne(''),
                          [nombre, COL_DEPTO, COL_ESTATUS,
                           COL_APROX_LUGAR, COL_APROX_DEPTO]]
            hoja.to_excel(writer, sheet_name=nombre, index=False,
                          freeze_panes=(1, 0))
            ws = writer.sheets[nombre]
            ws.auto_filter.ref = ws.dimensions
            ws.column_dimensions['A'].width = 55 if nombre == COL_PRODUCTO else 30
            ws.column_dimensions['B'].width = 29
            ws.column_dimensions['C'].width = 19
            ws.column_dimensions['D'].width = 58
            ws.column_dimensions['E'].width = 29


def ejecutar(archivo: Path, *, visible: bool = False):
    if not archivo.exists():
        raise FileNotFoundError(archivo)
    tabla = pd.read_excel(archivo, dtype=str, keep_default_na=False,
                          engine='openpyxl')
    if list(tabla.columns[:2]) != [COL_TERRITORIO, COL_PRODUCTO]:
        raise ValueError('El archivo debe comenzar con: '
                         f'{COL_TERRITORIO}, {COL_PRODUCTO}')
    filas = [{COL_TERRITORIO: txt(r[COL_TERRITORIO]),
              COL_PRODUCTO: txt(r[COL_PRODUCTO]),
              COL_DEPTO: '', COL_ESTATUS: 'PENDIENTE',
              COL_APROX_LUGAR: '', COL_APROX_DEPTO: ''}
             for _, r in tabla.iterrows()]
    salida = archivo.with_name(f'{archivo.stem}_DepartamentosColombia_Maps.xlsx')
    print(f'{len(filas)} registros. Fuente ÚNICA: Google Maps.')

    navegador = None
    cache = {}
    try:
        for i, row in enumerate(filas, 1):
            territorio, producto = row[COL_TERRITORIO], row[COL_PRODUCTO]
            if not (territorio or producto):
                row[COL_ESTATUS] = 'NO ENCONTRADO'
                continue
            if navegador is None:
                navegador = crear_driver(headless=HEADLESS and not visible)
            try:
                depto, evidencia, lugar_aprox, depto_aprox = resolver_maps(
                    navegador, territorio, producto, cache)
            except CaptchaDetectado as exc:
                print(f'CAPTCHA en fila {i}. Se detiene y guarda avance: {exc}')
                break  # La fila actual permanece PENDIENTE.
            except Exception as exc:
                depto, lugar_aprox, depto_aprox = '', '', ''
                evidencia = f'Error Maps: {type(exc).__name__}: {exc}'
            row[COL_DEPTO] = norm(depto) if depto else ''
            row[COL_APROX_LUGAR] = lugar_aprox if not depto else ''
            row[COL_APROX_DEPTO] = norm(depto_aprox) if not depto else ''
            row[COL_ESTATUS] = 'ENCONTRADO' if depto else 'NO ENCONTRADO'
            print(f'[{i}/{len(filas)}] {territorio or producto} -> '
                  f'{row[COL_DEPTO] or "SIN CONFIRMAR"} ({evidencia})'
                  f' | sugerencia: {row[COL_APROX_LUGAR]} / {row[COL_APROX_DEPTO]}')
            if i % GUARDAR_CADA == 0:
                guardar(filas, salida)
    except KeyboardInterrupt:
        print('Proceso interrumpido. Conservando resultados obtenidos.')
    finally:
        guardar(filas, salida)
        if navegador is not None:
            navegador.quit()
    print('Resumen:', pd.DataFrame(filas)[COL_ESTATUS].value_counts().to_dict())
    print('Guardado en:', salida)
    return salida


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('excel', nargs='?', type=Path,
                        help='Archivo Excel, o selector si no se proporciona.')
    parser.add_argument('--visible', action='store_true',
                        help='Abre Chrome visible para depurar Maps/CAPTCHA.')
    args = parser.parse_args()
    ejecutar(args.excel or seleccionar_archivo(), visible=args.visible)


if __name__ == '__main__':
    main()
