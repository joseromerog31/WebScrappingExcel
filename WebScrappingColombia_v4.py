from __future__ import annotations

import argparse
import ctypes
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
COLUMNAS = [COL_TERRITORIO, COL_PRODUCTO, COL_DEPTO, COL_ESTATUS]

HEADLESS = True
PAUSA_MAPS = 1.3
GUARDAR_CADA = 10
MAX_CONSULTAS_TERRITORIO = 3
MAX_CONSULTAS_PRODUCTO = 4

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
    s = unicodedata.normalize('NFKD', txt(valor).upper())
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9]+', ' ', s)).strip()


def palabra_entera(texto: str, termino: str) -> bool:
    return bool(re.search(r'(?<![A-Z0-9])' + re.escape(norm(termino)) +
                          r'(?![A-Z0-9])', norm(texto)))


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
            (f'{valor}, departamento, Colombia', 'territorio', valor),
        ]
        limite = MAX_CONSULTAS_TERRITORIO
    else:
        limpio = descomponer_nombre(producto)
        if not limpio:
            return []
        pista = extraer_pista(producto)
        # En códigos operativos MDT que equivalen a una ciudad, buscar como territorio geográfico en Maps, no como establecimiento comercial.
        solo_ubicacion_mdt = (
            bool(re.search(r'(?:^|_)MDT_', txt(producto), flags=re.I)) and
            pista and norm(pista) == norm(limpio)
        )
        candidatos = [(f'{limpio}, Colombia',
                       'lugar_pista' if solo_ubicacion_mdt else 'empresa',
                       limpio)]
        if pista and norm(pista) != norm(limpio):
            # Primero intentar referencia geográfica más precisa con el centro.
            candidatos.append((f'{limpio}, {pista}, Colombia', 'empresa', limpio))
            # Último recurso: ubicación indicada dentro del nombre del centro.
            candidatos.append((f'{pista}, Colombia', 'lugar_pista', pista))
            candidatos.append((f'municipio de {pista}, Colombia', 'lugar_pista', pista))
        else:
            # Versión breve, quitando terminaciones societarias que Maps ignora.
            abreviado = re.sub(r'\b(?:S\s*\.?\s*A\s*\.?\s*S\s*\.?|S\s*\.?\s*A\s*\.?|LTDA)\b.*$',
                               '', limpio, flags=re.I).strip(' -.')
            if abreviado and norm(abreviado) != norm(limpio):
                candidatos.append((f'{abreviado}, Colombia', 'empresa', abreviado))
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
    """
    Orden: categoría geográfica explícita > posición junto a 'Colombia' >
    último componente de una dirección. Evita interpretar la ciudad 'Caldas'
    como el departamento Caldas en 'Caldas, Antioquia, Colombia'.
    """
    texto = unquote(txt(evidencia)).replace('+', ' ')
    s = norm(texto)
    if not s or _evidencia_extranjera(texto):
        return ''

    # Categorías de las fichas de municipios, como "Municipio en Huila, Colombia".
    patrones = (
        r'\b(?:MUNICIPIO|CIUDAD|LOCALIDAD|PUEBLO|DISTRITO|POBLACION)\s+' 
        r'(?:UBICADO\s+)?(?:EN|DEL|DE)\s+(?:EL\s+)?(?:DEPARTAMENTO\s+(?:DE|DEL)\s+)?'
        r'([A-Z ]+?)(?:\s+COLOMBIA|$)',
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

    # "Pitalito, Huila, Colombia"; "Caldas, Antioquia, Colombia".
    # También se maneja URL de ficha: /maps/place/Pitalito,+Huila,+Colombia/.
    componentes = [norm(x) for x in re.split(r'[,;|\n]', texto)]
    componentes = [x for x in componentes if x]
    for i, componente in enumerate(componentes):
        if componente == 'COLOMBIA' and i > 0:
            # "Caldas, Colombia" o "Sucre, Colombia" NO confirman un
            # departamento: pueden describir un municipio homónimo.
            if i == 1 and canonico(componentes[0]) and \
                    componentes[0] not in ('BOGOTA', 'BOGOTA DC', 'BOGOTA D C'):
                return ''
            return canonico(componentes[i - 1])
    if len(componentes) >= 2:
        # Dirección localizada que omite la palabra Colombia.
        d = canonico(componentes[-1])
        if d:
            return d

    # Una ubicación de Bogotá puede aparecer solo como "Bogotá, Colombia".
    return ''


def lugar_coincide(objetivo: str, nombre: str, tipo: str,
                   evidencias: list[tuple[str, str]]) -> bool:
    objetivo_n = norm(objetivo)
    nombre_n = norm(nombre)
    if not nombre_n:
        return False

    if tipo in ('territorio', 'lugar_pista'):
        # Aceptar "Pitalito" y "Municipio de Pitalito", no "Restaurante Pitalito".
        variantes = {objetivo_n, 'MUNICIPIO DE ' + objetivo_n,
                     'MUNICIPIO ' + objetivo_n, 'CIUDAD DE ' + objetivo_n}
        titulo_correcto = (nombre_n in variantes or
                           nombre_n.startswith(objetivo_n + ' ') or
                           nombre_n.startswith('MUNICIPIO DE ' + objetivo_n + ' ') or
                           nombre_n.startswith('CIUDAD DE ' + objetivo_n + ' '))
        if not titulo_correcto:
            return False
        categorias = [norm(v) for fuente, v in evidencias if fuente == 'categoria']
        if any(any(palabra_entera(c, x) for x in (
                   'RESTAURANTE', 'HOTEL', 'CAFETERIA', 'HOSPITAL', 'FARMACIA',
                   'TIENDA', 'GIMNASIO', 'BANCO', 'EMPRESA', 'CLINICA', 'BAR'))
               for c in categorias):
            return False
        # En ficha de municipio sin categoría, la URL o dirección debe conservar
        # también el nombre de ubicación; evita negocios de nombre coincidente.
        return any(palabra_entera(v, objetivo_n) for fuente, v in evidencias
                   if fuente in ('direccion', 'url', 'subtitulo', 'categoria')) or \
               any(any(palabra_entera(c, k) for k in CATEGORIAS_GEOGRAFICAS)
                   for c in categorias)

    # Centros de salud / nombres comerciales: conservar validación por tokens.
    tokens = {t for t in objetivo_n.split() if len(t) >= 4
              and t not in PALABRAS_GENERICAS}
    if not tokens:
        return False
    iguales = tokens & set(nombre_n.split())
    # Dos términos propios evitan aceptar un municipio como si fuese la
    # ficha del hospital solo porque comparten una palabra (p. ej. CALDAS).
    minimo = 2 if len(tokens) >= 2 else 1
    return len(iguales) >= minimo


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
    _, TimeoutException, _, By, WebDriverWait = importar_selenium()
    url = 'https://www.google.com/maps/search/?api=1&query=' + quote_plus(consulta)
    driver.get(url)
    try:
        WebDriverWait(driver, 14).until(
            lambda d: bool(_nombre_ficha(d, By)) or
            bool(d.find_elements(By.CSS_SELECTOR, 'a.hfpxzc')) or
            bool(d.find_elements(By.CSS_SELECTOR, 'div[role="feed"]')))
    except TimeoutException:
        pass
    cerrar_consentimiento(driver, By)
    if detectar_captcha(driver, By):
        raise CaptchaDetectado('Maps solicitó CAPTCHA/verificación manual.')

    if not _nombre_ficha(driver, By):
        # En búsquedas con múltiples sitios, abrir la primera ficha.
        for sel in ('a.hfpxzc', 'div[role="feed"] a[href*="/maps/place/"]'):
            try:
                enlaces = [x for x in driver.find_elements(By.CSS_SELECTOR, sel)
                           if x.is_displayed()]
                if enlaces:
                    enlaces[0].click()
                    WebDriverWait(driver, 7).until(
                        lambda d: bool(_nombre_ficha(d, By)))
                    break
            except Exception:
                continue

    if detectar_captcha(driver, By):
        raise CaptchaDetectado('Maps solicitó CAPTCHA/verificación manual.')
    nombre = _nombre_ficha(driver, By)
    if not nombre:
        return FichaMaps('', [], driver.current_url)

    # Dar tiempo al panel para completar dirección y categoría.
    try:
        WebDriverWait(driver, 3).until(lambda d: bool(d.find_elements(
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

    for sel in ('button[data-item-id="address"]', '[data-item-id="address"]',
                'button[aria-label^="Dirección:"]',
                'button[aria-label^="Address:"]'):
        try:
            for x in driver.find_elements(By.CSS_SELECTOR, sel):
                if x.is_displayed():
                    dato = x.get_attribute('aria-label') or x.text
                    anadir('direccion', re.sub(
                        r'^(?:Dirección|Address):\s*', '', dato, flags=re.I))
        except Exception:
            pass

    # Subtítulo geográfico de lugares administrativos: municipio en Huila...
    for sel in ('div.DkEaL', '[class*="fontBodyMedium"]',
                '[class*="fontBodySmall"]'):
        try:
            for x in driver.find_elements(By.CSS_SELECTOR, sel)[:24]:
                if not x.is_displayed():
                    continue
                s = txt(x.text)
                if len(s) > 180:
                    continue
                # Descarta textos de navegación, publicidad y reseñas.
                if (palabra_entera(s, 'COLOMBIA') or
                    any(palabra_entera(s, z) for z in (
                        'MUNICIPIO', 'CIUDAD EN', 'DEPARTAMENTO', 'CITY IN',
                        'MUNICIPALITY', 'TOWN IN', 'PUEBLO EN', 'LOCALIDAD EN'))):
                    anadir('subtitulo', s)
                elif sel == 'div.DkEaL':
                    anadir('categoria', s)
        except Exception:
            continue

    # Texto de encabezado/etiquetas puede ser la única referencia regional.
    for sel in ('[aria-label^="Ubicado en"]', '[aria-label^="Located in"]'):
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel)[:6]:
                anadir('subtitulo', el.get_attribute('aria-label'))
        except Exception:
            pass

    anadir('url', extraer_url_ubicacion(driver.current_url))
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


def resolver_maps(driver, territorio: str, producto: str,
                  cache: dict[str, FichaMaps]) -> tuple[str, str]:
    ultimo_motivo = 'No hay términos para consultar'
    for consulta, tipo, objetivo in consultas_para_maps(territorio, producto):
        print(f'    Maps [{tipo}]: {consulta}')
        if consulta not in cache:
            cache[consulta] = consultar_maps(driver, consulta)
            time.sleep(PAUSA_MAPS)
        departamento, motivo = evaluar_ficha(cache[consulta], tipo, objetivo)
        if departamento:
            return departamento, motivo
        ultimo_motivo = motivo
        print(f'      Sin confirmar: {motivo}')
    return '', ultimo_motivo


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
    # Dos hojas, tres columnas cada una.
    df = pd.DataFrame(filas, columns=COLUMNAS).fillna('')
    with pd.ExcelWriter(ruta, engine='openpyxl') as writer:
        for nombre in (COL_TERRITORIO, COL_PRODUCTO):
            hoja = df.loc[df[nombre].astype(str).str.strip().ne(''),
                          [nombre, COL_DEPTO, COL_ESTATUS]]
            hoja.to_excel(writer, sheet_name=nombre, index=False,
                          freeze_panes=(1, 0))
            ws = writer.sheets[nombre]
            ws.auto_filter.ref = ws.dimensions
            ws.column_dimensions['A'].width = 55 if nombre == COL_PRODUCTO else 30
            ws.column_dimensions['B'].width = 29
            ws.column_dimensions['C'].width = 19


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
              COL_DEPTO: '', COL_ESTATUS: 'PENDIENTE'}
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
                depto, evidencia = resolver_maps(navegador, territorio, producto, cache)
            except CaptchaDetectado as exc:
                print(f'CAPTCHA en fila {i}. Se detiene y guarda avance: {exc}')
                break  # La fila actual permanece PENDIENTE.
            except Exception as exc:
                depto = ''
                evidencia = f'Error Maps: {type(exc).__name__}: {exc}'
            row[COL_DEPTO] = depto
            row[COL_ESTATUS] = 'ENCONTRADO' if depto else 'NO ENCONTRADO'
            print(f'[{i}/{len(filas)}] {territorio or producto} -> '
                  f'{depto or "SIN CONFIRMAR"} ({evidencia})')
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
