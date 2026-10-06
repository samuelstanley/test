"""Configuration: paths, conversions, source registry, classification rules.

Everything a user may need to tune lives here. Nothing in this file is data:
there are no hard-coded volumes, only URLs, unit factors and name-matching rules.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- paths
ROOT = Path(os.environ.get("LATAM_GAS_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
CACHE_DIR = DATA_DIR / "cache"
INPUTS_DIR = ROOT / "inputs"
OUTPUT_DIR = ROOT / "output"

START_DATE = date(2021, 1, 1)

MAIN_WORKBOOK = "latam_gas_snd_balance.xlsx"
DRILLDOWN_WORKBOOKS = {
    "Argentina": "latam_gas_snd_argentina.xlsx",
    "Brazil": "latam_gas_snd_brazil.xlsx",
    "Colombia": "latam_gas_snd_colombia.xlsx",
}
BRAZIL_POINT_PARQUET = "brazil_point_days.parquet"

# Strict reading of hard rule 3: when a source fails this run, its lines are blank
# even if earlier runs archived data for it. Set True to fall back to the archive.
USE_ARCHIVE_ON_FAILURE = False

HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) latam-gas-snd/1.0 (+data research; contact repo owner)"

# --------------------------------------------------------------------------- conversions
# ENARGAS publishes thousand m3 (miles de m3).
ENARGAS_KM3_TO_MCM = 1 / 1000
# Colombia: GBTU x 0.0283 = mcm. Colombian "MBTU" is a million Btu, i.e. 1/1000 GBTU.
GBTU_TO_MCM = 0.0283
MBTU_TO_MCM = GBTU_TO_MCM / 1000
KPC_TO_MCM = 28.316846592 / 1e6          # thousand cubic feet (KPC) -> mcm
MPC_TO_MCM = 28.316846592 / 1e3          # million cubic feet (MPC / MMcf) -> mcm
# LNG
LNG_M3_TO_GAS_M3 = 585
LNG_TONNE_TO_GAS_M3 = 1360
# ONS power burn memo: MWh x 3.6 GJ/MWh / 45% efficiency / 0.0389 GJ/m3
ONS_GJ_PER_MWH = 3.6
ONS_EFFICIENCY = 0.45
ONS_GJ_PER_M3 = 0.0389


def ons_mwh_to_mcm(mwh: float) -> float:
    return mwh * ONS_GJ_PER_MWH / ONS_EFFICIENCY / ONS_GJ_PER_M3 / 1e6


# --------------------------------------------------------------------------- URLs
ENARGAS_BASE = "https://www.enargas.gob.ar/secciones/transporte-y-distribucion/"
ENARGAS_PARTES = ENARGAS_BASE + "dod-partes-exp-imp-consulta.php"
ENARGAS_CHART_ITEMS = ENARGAS_BASE + "dod-graficos-de-programacion-items.php"
ENARGAS_CHARTS_PAGE = ENARGAS_BASE + "dod-graficos-de-programacion.php"
ENARGAS_QUERY_CACHE = DATA_DIR / "enargas_query.json"

ANP_PAGE = (
    "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/"
    "dados-consolidados-movimentacao-de-gas-natural-em-gasodutos-de-transporte"
)
BR_POINT_OVERRIDES = INPUTS_DIR / "br_point_overrides.csv"

ONS_BUCKET = "https://ons-aws-prod-opendata.s3.amazonaws.com"
ONS_PREFIX = "dataset/geracao_usina_2_ho/"
ONS_GAS_FUELS = ("Gás",)   # nom_tipocombustivel values counted as gas-fired

BMC_BASE = "https://www.bmcbec.com.co/"
BMC_INDEX = BMC_BASE + "informaci%C3%B3n-operativa"
# Each BMC report: page URL (if known) and title used to find the page on the index
# when the URL is missing or moved. Only "energia_inyectada" URL is confirmed in the
# requirements; the others are located by title on the index page.
BMC_REPORTS = {
    "bmc_injection": {
        "title": "Energía inyectada",
        "url": BMC_BASE + "informaci%C3%B3n-operativa/energ%C3%ADa-inyectada",
        "keywords": ["inyect", "punto", "entrada"],
    },
    "bmc_imported_not_injected": {
        "title": "Cantidad declarada por comercializadores de gas importado y no inyectada al SNT",
        "url": None,
        "keywords": ["importado", "no inyect", "declarad"],
    },
    "bmc_offtake_snt": {
        "title": "Cantidad de energía tomada diariamente del SNT",
        "url": None,
        "keywords": ["tomada", "sector", "demanda"],
    },
    "bmc_offtake_marketers": {
        "title": "Energía tomada por comercializadores",
        "url": None,
        "keywords": ["tomada", "comercializador"],
    },
    "bmc_offtake_tramo": {
        "title": "Energía tomada transportadores por tramo",
        "url": None,
        "keywords": ["tramo", "transportador"],
    },
}
# Pin the Power BI visual for a report once confirmed with
#   python -m latam_gas_snd.pbi <page url>
# e.g. "bmc_injection": {"page": "Energía Inyectada", "visual": "a1b2c3d4e5"}
BMC_PINS: dict[str, dict[str, str]] = {}

POWERBI_ROUTING = "https://api.powerbi.com/public/routing/cluster/{tenant}"

XM_API = "https://servapibi.xm.com.co"
XM_METRIC = "ConsCombustibleMBTU"
XM_ENTITY = "Recurso"
XM_CHUNK_DAYS = 30

KPLER_EMAIL_ENV = "KPLER_EMAIL"
KPLER_PASSWORD_ENV = "KPLER_PASSWORD"
KPLER_CHILE_ZONE = "Chile"

# --------------------------------------------------------------------------- source registry
# id -> metadata shown on the Sources sheet and under every column it feeds.
SOURCES: dict[str, dict[str, str]] = {
    "enargas_imports": {
        "name": "ENARGAS partes: importaciones",
        "publisher": "ENARGAS (ENReGE)",
        "url": ENARGAS_PARTES + "?tipo=importaciones",
        "frequency": "daily",
        "unit": "thousand m3/d (/1000 = mcm/d)",
        "notes": "Imports from Bolivia and Chile, LNG Escobar and Bahía Blanca, by route.",
    },
    "enargas_exp_dentro": {
        "name": "ENARGAS partes: exportaciones dentro del sistema",
        "publisher": "ENARGAS (ENReGE)",
        "url": ENARGAS_PARTES + "?tipo=exp_dentro",
        "frequency": "daily",
        "unit": "thousand m3/d (/1000 = mcm/d)",
        "notes": "Exports through the transport system, by route.",
    },
    "enargas_exp_fuera": {
        "name": "ENARGAS partes: exportaciones fuera del sistema",
        "publisher": "ENARGAS (ENReGE)",
        "url": ENARGAS_PARTES + "?tipo=exp_fuera",
        "frequency": "daily",
        "unit": "thousand m3/d (/1000 = mcm/d)",
        "notes": "Exports by producer lines (production outside the transport system), by route.",
    },
    "enargas_injection": {
        "name": "ENARGAS gráficos de programación: inyección (cat=6)",
        "publisher": "ENARGAS (ENReGE)",
        "url": ENARGAS_CHART_ITEMS + "?cat=6",
        "frequency": "daily (read from chart PDFs)",
        "unit": "thousand m3/d unless chart axis says otherwise",
        "notes": "Injection into the transport system by pipeline; vector PDF chart parsed by colour.",
    },
    "enargas_demand": {
        "name": "ENARGAS gráficos de programación: demanda (cat=8)",
        "publisher": "ENARGAS (ENReGE)",
        "url": ENARGAS_CHART_ITEMS + "?cat=8",
        "frequency": "daily (read from chart PDFs)",
        "unit": "thousand m3/d unless chart axis says otherwise",
        "notes": "Priority, industry, power, CNG; vector PDF chart parsed by colour.",
    },
    "enargas_linepack": {
        "name": "ENARGAS gráficos de programación: line pack",
        "publisher": "ENARGAS (ENReGE)",
        "url": ENARGAS_CHARTS_PAGE,
        "frequency": "daily (read from chart)",
        "unit": "thousand m3 level; change derived day on day",
        "notes": "Linepack change = level(d) - level(d-1); blank if either day missing.",
    },
    "anp": {
        "name": "ANP movimentação de gás natural em gasodutos de transporte",
        "publisher": "ANP",
        "url": ANP_PAGE,
        "frequency": "daily values in monthly CSVs, ~1 month lag",
        "unit": "as stated in file (thousand m3/d expected)",
        "notes": "Volume Realizado and Empacotamento by point, all transporters.",
    },
    "ons": {
        "name": "ONS geração por usina (geracao_usina_2_ho)",
        "publisher": "ONS open data (AWS S3)",
        "url": f"{ONS_BUCKET}/{ONS_PREFIX}",
        "frequency": "hourly, summed to daily",
        "unit": "MWmed; gas-eq = MWh x 3.6 / 45% / 0.0389 GJ/m3",
        "notes": "Memo only: gas-fired generation converted to gas equivalent.",
    },
    "bmc_injection": {
        "name": "BMC Gestor del Mercado: Energía inyectada",
        "publisher": "Bolsa Mercantil de Colombia (Gestor del Mercado de Gas)",
        "url": BMC_REPORTS["bmc_injection"]["url"],
        "frequency": "daily (Power BI, rolling ~12 months, archived each run)",
        "unit": "MBTU/d (million Btu); x 0.0283/1000 = mcm/d",
        "notes": "Injection by entry point, production vs imports.",
    },
    "bmc_imported_not_injected": {
        "name": "BMC: gas importado declarado y no inyectado al SNT",
        "publisher": "Bolsa Mercantil de Colombia (Gestor del Mercado de Gas)",
        "url": BMC_INDEX,
        "frequency": "daily (Power BI, archived each run)",
        "unit": "MBTU/d",
        "notes": "Imported gas used outside the SNT.",
    },
    "bmc_offtake_snt": {
        "name": "BMC: cantidad de energía tomada diariamente del SNT",
        "publisher": "Bolsa Mercantil de Colombia (Gestor del Mercado de Gas)",
        "url": BMC_INDEX,
        "frequency": "daily (Power BI, archived each run)",
        "unit": "MBTU/d",
        "notes": "Offtake from the national transport system by sector where published.",
    },
    "bmc_offtake_marketers": {
        "name": "BMC: energía tomada por comercializadores",
        "publisher": "Bolsa Mercantil de Colombia (Gestor del Mercado de Gas)",
        "url": BMC_INDEX,
        "frequency": "daily (Power BI, archived each run)",
        "unit": "MBTU/d",
        "notes": "Cross-check of offtake by marketer (memo).",
    },
    "bmc_offtake_tramo": {
        "name": "BMC: energía tomada transportadores por tramo",
        "publisher": "Bolsa Mercantil de Colombia (Gestor del Mercado de Gas)",
        "url": BMC_INDEX,
        "frequency": "daily (Power BI, archived each run)",
        "unit": "MBTU/d",
        "notes": "Cross-check of offtake by transporter and segment (memo).",
    },
    "xm": {
        "name": "XM API: ConsCombustibleMBTU (declared thermal fuel burn)",
        "publisher": "XM (Colombian system operator)",
        "url": XM_API,
        "frequency": "daily",
        "unit": "MBTU/d",
        "notes": "Power demand if BMC has no thermal split, else memo.",
    },
    "kpler": {
        "name": "Kpler LNG flows (imports to Chile)",
        "publisher": "Kpler (subscription)",
        "url": "kpler.sdk Flows.get",
        "frequency": "daily cargo discharges",
        "unit": "m3 LNG x 585 = m3 gas",
        "notes": "Cargo arrivals, not regas sendout: expect timing mismatch vs demand.",
    },
}

# --------------------------------------------------------------------------- ENARGAS route rules
# Ordered (pattern, counterparty). Patterns are case/accent-insensitive regexes.
ENARGAS_IMPORT_ROUTES = [
    (r"escobar", "LNG Escobar"),
    (r"bahia ?blanca|bahia", "LNG Bahia Blanca"),
    (r"gnl|regasif|lng", "LNG other"),
    (r"bolivia|ypfb|juana azurduy|gja|campo duran|madrejones|yacuiba|pocitos", "Bolivia"),
    (r"chile|gas ?andes|norandino|atacama|pacifico|methanex|magallanes", "Chile"),
]
ENARGAS_EXPORT_ROUTES = [
    (r"por bolivia|bolivia|ypfb", "Brazil via Bolivia"),
    (r"uruguaiana|brasil|brazil|tsb|aldea brasilera", "Brazil"),
    (r"cruz del sur|petro ?uruguay|uruguay|casablanca", "Uruguay"),
    (r"chile|gas ?andes|norandino|atacama|pacifico|methanex|magallanes|condor|posesion|"
     r"dungeness|bandurria|tierra del fuego|cullen|san sebastian", "Chile"),
]
ENARGAS_METHANEX = r"methanex"

# Chart series classification (ENARGAS cat=6 injection / cat=8 demand)
ENARGAS_INJECTION_CHART = r"inyecci|gasoducto"
ENARGAS_DEMAND_CHART = r"demanda|consumo|segment"
ENARGAS_INJECTION_EXCLUDE = r"gnl|escobar|bahia|bolivia|import|chile|total"
ENARGAS_DEMAND_SECTORS = [
    (r"prioritari|residencial|r ?\+ ?p|comercial", "Priority (residential, commercial)"),
    (r"industri", "Industry"),
    (r"usina|central|generaci|termic|cte|electric", "Power"),
    (r"gnc", "CNG"),
]
ENARGAS_DEMAND_EXCLUDE = r"export|total"     # exports come from partes; total is a check
ENARGAS_LINEPACK = r"line ?pack|linepack|empaque"
ENARGAS_LINEPACK_EXCLUDE = r"min|max|limite|objetivo|banda"

# --------------------------------------------------------------------------- ANP point rules
# Ordered (category, regex on normalised "point | transporter" text). Interconnections are
# dropped from both sides of the balance and reported in BR checks.
BR_CATEGORIES = [
    ("interconnection", r"interconex|interligac|\bpi[ -]|ponto de transferencia|transferencia entre"),
    ("biomethane", r"biometano|biogas"),
    ("lng", r"\bgnl\b|regaseific|terminal de gnl|\btrba\b|pecem|baia de guanabara|baia de todos|"
            r"\bacu\b|itaqui|sao francisco do sul|\btgs\b|barcarena|sergipe"),
    ("bolivia_border", r"corumba|caceres|mutun|san matias"),
    ("uruguaiana", r"uruguaiana"),
    ("fertiliser", r"fafen|fertiliz|\bufn\b|ansa|nitrogenad"),
    ("refinery", r"refinaria|\breduc\b|\breplan\b|\brevap\b|\bregap\b|\brepar\b|\brefap\b|\brlam\b|"
                 r"\brpbc\b|\brnest\b|\brecap\b|\breman\b|lubnor|\bsix\b"),
    ("power", r"\bute\b|termel|termoel|usina|termica"),
    ("production", r"\bupgn\b|\butgc?\b|cacimbas|cabiunas|caraguatatuba|\brota\b|estacao de tratamento|"
                   r"\bcampo\b|\bpolo\b|lagoa parda|atalaia|\bcatu\b|guamare|candeias|pilar|urucu|"
                   r"itaborai|gaslub|producao|processamento"),
    ("city_gate", r"city ?gate|\bcg\b|estacao de entrega|distribuidora|comgas|\bceg\b|gasmig|bahiagas|"
                  r"sulgas|compagas|scgas|msgas|copergas|pbgas|potigas|algas|sergas|cegas|gasbrasiliano|"
                  r"naturgy|es gas|cigas|necta|gas natural sp sul"),
    ("export", r"exporta"),
]
BR_RECEIPT_CATEGORIES = ("production", "lng", "bolivia_border", "uruguaiana", "biomethane")
BR_DELIVERY_CATEGORIES = ("power", "refinery", "fertiliser", "city_gate", "export")
BR_TRANSFER_TOLERANCE_MCM = 0.5     # |net internal transfers| above this is flagged
BR_BOLIVIA_NEGATIVE_TOLERANCE_MCM = 0.2

# --------------------------------------------------------------------------- Colombia rules
CO_IMPORT_ENTRY = r"spec|regasif|gnl|lng|importa|venezuela|ballena import|cartagena import"
CO_SECTORS = [
    (r"termo|termic|generaci|electric", "Power"),
    (r"industri", "Industry"),
    (r"residenc|comercial|domicili|distribuc", "Residential & commercial"),
    (r"gnv|vehicul|gnc", "CNG (GNV)"),
    (r"refiner", "Refinery"),
    (r"petroqu", "Petrochemical"),
    (r"compres|transport", "Transport fuel"),
]

# Tolerances for checks
PARTES_TOTAL_TOLERANCE_KM3 = 1.0        # route sum vs Total column, thousand m3
CHART_TOTAL_TOLERANCE_REL = 0.02        # chart series vs chart total (2%)
SOURCE_LAG_WARN_DAYS = 10               # end-date mismatch between sources in a country
