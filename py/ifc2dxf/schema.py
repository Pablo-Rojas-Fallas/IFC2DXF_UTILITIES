"""JSON analysis schema 2.0: structure, types, allowed values, synonyms and empty template.

Version 2.0 is the English version of schema 1.0. Files written with the Spanish 1.0 keys and values
are still read: every Spanish key and value is registered below as a synonym.
"""
from __future__ import annotations

import copy

VERSION = "2.0"

# Allowed values
ENUMS = {
    "suggested_type": ["stormwater", "sewer", "drainage", "water_supply", "electrical", "other"],
    "model_coordinates": ["global", "local"],
    "confidence": ["high", "medium", "low"],
    "severity": ["high", "medium", "low"],
    "axis_mode": ["2D", "3D"],
    "layers": ["per_system", "single"],
}

# Value synonyms (already normalised: lower case, no accents, '_' as separator)
VALUE_SYNONYMS = {
    "suggested_type": {
        "stormwater": ["pluvial", "pluviales", "storm_water", "storm", "overvann", "ov", "surface_water", "aguas_pluviales",
                       "lluvia", "rain", "rainwater"],
        "sewer": ["saneamiento", "sewage", "spillvann", "sp", "aguas_residuales", "residuales", "fecales", "waste_water",
                  "wastewater", "combinado", "combined", "unitaria", "af", "alcantarillado", "foul", "foul_water"],
        "drainage": ["drenaje", "drain", "drens", "dr", "drenajes", "land_drainage"],
        "water_supply": ["abastecimiento", "water", "agua", "agua_potable", "vannledning", "vann", "vl", "potable",
                         "drinking_water", "suministro", "potable_water"],
        "electrical": ["electrica", "electric", "electricidad", "el", "cable", "cables", "power", "energia", "telecom",
                       "alumbrado", "lighting"],
        "other": ["otro", "otros", "otra", "misc", "varios"],
    },
    "model_coordinates": {
        "global": ["globales", "absolutas", "absolutes", "absolute", "reales", "mapa", "map", "georreferenciadas",
                   "georeferenced", "world", "projected"],
        "local": ["locales", "relativas", "relative", "relativo", "engineering", "proyecto", "project"],
    },
    "confidence": {"high": ["alta", "alto", "h"], "medium": ["media", "medio", "moderada", "moderate", "m"],
                   "low": ["baja", "bajo", "l"]},
    "severity": {"high": ["alta", "alto", "critical", "critica", "grave", "severe", "h"],
                 "medium": ["media", "medio", "moderada", "moderate", "m"],
                 "low": ["baja", "bajo", "leve", "menor", "minor", "l", "info", "informativa", "informational"]},
    "axis_mode": {"2D": ["2d", "2_d", "plano", "plan", "z0", "z_0", "2d_z0", "planta", "2_dimensiones", "flat"],
                  "3D": ["3d", "3_d", "cota_real", "con_cota", "3_dimensiones", "elevacion", "elevation", "real_elevation"]},
    "layers": {"per_system": ["por_sistema", "por_sistemas", "sistema", "sistemas", "by_system", "system", "systems",
                              "separadas", "separate", "separar", "multiples", "multiple"],
               "single": ["unica", "una", "una_sola", "capa_unica", "one", "sola", "todo_en_una", "single_layer"]},
}

_PROP = {"pset": "str", "property": "str", "coverage_pct": "pct", "unit": "str"}
_RULE = {"ifc_classes": "classes", "condition": "str"}

# Leaf types: str, bool, num, num_m (length in metres), int, pct, classes, unit, ("enum", name)
SCHEMA = {
    "schema_version": "str",
    "file": {"name": "str", "ifc_schema": "str", "source_application": "str", "length_unit": "unit"},
    "georeferencing": {
        "has_ifcmapconversion": "bool", "coordinate_system": "str",
        "eastings": "num_m", "northings": "num_m", "orthogonal_height": "num_m",
        "x_axis_abscissa": "num", "x_axis_ordinate": "num", "scale": "num", "map_unit": "unit",
        "model_coordinates": ("enum", "model_coordinates"),
        "xy_range": {"xmin": "num_m", "xmax": "num_m", "ymin": "num_m", "ymax": "num_m"},
        "confidence": ("enum", "confidence"),
    },
    "systems": [{"ifc_name": "str", "suggested_type": ("enum", "suggested_type"), "n_pipes": "int",
                 "n_manholes": "int", "n_accessories": "int", "how_to_identify": "str"}],
    "detection_rules": {"pipes": dict(_RULE), "manholes": dict(_RULE), "accessories": dict(_RULE)},
    "properties": {k: dict(_PROP) for k in ("diameter", "material", "invert_start", "invert_end", "pipe_id", "manhole_id")},
    "anomalies": [{"description": "str", "severity": ("enum", "severity")}],
    "recommendations": {"axis_mode": ("enum", "axis_mode"), "layers": ("enum", "layers"), "notes": "str"},
}

# Key synonyms (normalised). A key may point to several: the one that exists at that level is used.
# Includes every Spanish key of schema 1.0.
KEY_SYNONYMS = {
    # root
    "version": ["schema_version"], "schema": ["schema_version"], "esquema_version": ["schema_version"],
    "version_esquema": ["schema_version"],
    "archivo": ["file"], "fichero": ["file"], "ifc": ["file"], "ifc_file": ["file"],
    "georreferenciacion": ["georeferencing"], "georeferenciacion": ["georeferencing"], "georreferencia": ["georeferencing"],
    "georef": ["georeferencing"], "georeference": ["georeferencing"], "georeferencing_info": ["georeferencing"],
    "sistemas": ["systems"], "sistema": ["systems"], "redes": ["systems"], "networks": ["systems"], "system": ["systems"],
    "reglas_deteccion": ["detection_rules"], "reglas": ["detection_rules"], "reglas_de_deteccion": ["detection_rules"],
    "deteccion": ["detection_rules"], "rules": ["detection_rules"], "detection": ["detection_rules"],
    "propiedades": ["properties"], "props": ["properties"], "property_mapping": ["properties"],
    "anomalias": ["anomalies"], "anomalias_detectadas": ["anomalies"], "problemas": ["anomalies"], "issues": ["anomalies"],
    "warnings": ["anomalies"], "avisos": ["anomalies"], "problems": ["anomalies"],
    "recomendaciones": ["recommendations"], "recomendacion": ["recommendations"], "recommendation": ["recommendations"],
    # file
    "nombre": ["name", "ifc_name"], "filename": ["name"], "file_name": ["name"], "nombre_archivo": ["name"],
    "esquema_ifc": ["ifc_schema"], "schema_ifc": ["ifc_schema"], "esquema": ["ifc_schema"], "version_ifc": ["ifc_schema"],
    "ifc_version": ["ifc_schema"],
    "programa_origen": ["source_application"], "software": ["source_application"], "programa": ["source_application"],
    "authoring_tool": ["source_application"], "origen": ["source_application"], "aplicacion": ["source_application"],
    "application": ["source_application"], "source": ["source_application"],
    "unidades_longitud": ["length_unit"], "units": ["length_unit"], "unidades": ["length_unit"],
    "unidad_longitud": ["length_unit"], "length_units": ["length_unit"],
    "unidad": ["unit", "length_unit"], "unit": ["length_unit"],
    # georeferencing
    "tiene_ifcmapconversion": ["has_ifcmapconversion"], "has_mapconversion": ["has_ifcmapconversion"],
    "ifcmapconversion": ["has_ifcmapconversion"], "tiene_map_conversion": ["has_ifcmapconversion"],
    "mapconversion": ["has_ifcmapconversion"], "has_map_conversion": ["has_ifcmapconversion"],
    "sistema_coordenadas": ["coordinate_system"], "crs": ["coordinate_system"], "sistema_de_coordenadas": ["coordinate_system"],
    "epsg": ["coordinate_system"], "src": ["coordinate_system"], "coordinate_reference_system": ["coordinate_system"],
    "easting": ["eastings"], "este": ["eastings"], "northing": ["northings"], "norte": ["northings"],
    "height": ["orthogonal_height"], "orthogonalheight": ["orthogonal_height"], "altura": ["orthogonal_height"],
    "xaxisabscissa": ["x_axis_abscissa"], "xaxisordinate": ["x_axis_ordinate"],
    "escala": ["scale"], "unidad_mapa": ["map_unit"], "mapunit": ["map_unit"],
    "coordenadas_en_modelo": ["model_coordinates"], "coordinates": ["model_coordinates"], "coordenadas": ["model_coordinates"],
    "tipo_coordenadas": ["model_coordinates"], "coordinates_in_model": ["model_coordinates"],
    "rango_xy": ["xy_range"], "range": ["xy_range"], "rango": ["xy_range"], "extent": ["xy_range"], "bbox": ["xy_range"],
    "extension": ["xy_range"], "bounding_box": ["xy_range"],
    "confianza": ["confidence"],
    # systems
    "nombre_en_ifc": ["ifc_name"], "name": ["ifc_name"], "system_name": ["ifc_name"], "nombre_sistema": ["ifc_name"],
    "name_in_ifc": ["ifc_name"],
    "tipo_sugerido": ["suggested_type"], "type": ["suggested_type"], "tipo": ["suggested_type"],
    "n_tuberias": ["n_pipes"], "pipes": ["n_pipes"], "num_pipes": ["n_pipes"], "num_tuberias": ["n_pipes"],
    "pipe_count": ["n_pipes"], "tuberias": ["pipes", "n_pipes"],
    "n_pozos": ["n_manholes"], "manholes": ["n_manholes"], "num_manholes": ["n_manholes"], "num_pozos": ["n_manholes"],
    "manhole_count": ["n_manholes"], "pozos": ["manholes", "n_manholes"],
    "n_accesorios": ["n_accessories"], "accessories": ["n_accessories"], "fittings": ["n_accessories", "accessories"],
    "n_fittings": ["n_accessories"], "num_accesorios": ["n_accessories"], "accesorios": ["accessories", "n_accessories"],
    "como_identificar": ["how_to_identify"], "identificacion": ["how_to_identify"], "criterio": ["how_to_identify"],
    "identification": ["how_to_identify"], "identify_by": ["how_to_identify"],
    # detection rules
    "clases_ifc": ["ifc_classes"], "classes": ["ifc_classes"], "clases": ["ifc_classes"],
    "condicion": ["condition"], "condiciones": ["condition"], "filtro": ["condition"], "filter": ["condition"],
    # properties
    "diametro": ["diameter"], "diametro_interior": ["diameter"], "inner_diameter": ["diameter"],
    "invert_inicio": ["invert_start"], "cota_inicio": ["invert_start"], "start_invert": ["invert_start"],
    "invert_fin": ["invert_end"], "cota_fin": ["invert_end"], "end_invert": ["invert_end"],
    "id_tramo": ["pipe_id"], "id_tuberia": ["pipe_id"], "segment_id": ["pipe_id"],
    "id_pozo": ["manhole_id"], "chamber_id": ["manhole_id"],
    "propiedad": ["property"], "property_name": ["property"], "nombre_propiedad": ["property"], "prop": ["property"],
    "property_set": ["pset"], "psetname": ["pset"], "pset_name": ["pset"], "conjunto": ["pset"],
    "cobertura_pct": ["coverage_pct"], "coverage": ["coverage_pct"], "cobertura": ["coverage_pct"],
    # anomalies
    "descripcion": ["description"], "detalle": ["description"], "detail": ["description"],
    "gravedad": ["severity"], "severidad": ["severity"], "nivel": ["severity"], "level": ["severity"],
    # recommendations
    "modo_ejes": ["axis_mode"], "modo": ["axis_mode"], "ejes": ["axis_mode"], "axes": ["axis_mode"], "mode": ["axis_mode"],
    "capas": ["layers"], "modo_capas": ["layers"], "layer_mode": ["layers"],
    "notas": ["notes"], "comentarios": ["notes"], "observaciones": ["notes"], "comments": ["notes"],
}


# Keys and values of the Spanish schema 1.0: translating them is not a "repair", it is reported once.
LEGACY_KEYS = {
    "archivo", "nombre", "esquema_ifc", "programa_origen", "unidades_longitud", "georreferenciacion",
    "tiene_ifcmapconversion", "sistema_coordenadas", "escala", "unidad_mapa", "coordenadas_en_modelo", "rango_xy",
    "confianza", "sistemas", "nombre_en_ifc", "tipo_sugerido", "n_tuberias", "n_pozos", "n_accesorios",
    "como_identificar", "reglas_deteccion", "tuberias", "pozos", "accesorios", "clases_ifc", "condicion",
    "propiedades", "diametro", "invert_inicio", "invert_fin", "id_tramo", "id_pozo", "propiedad", "cobertura_pct",
    "unidad", "anomalias", "descripcion", "gravedad", "recomendaciones", "modo_ejes", "capas", "notas",
}
LEGACY_VALUES = {"pluvial", "saneamiento", "drenaje", "abastecimiento", "electrica", "otro", "globales", "locales",
                 "alta", "media", "baja", "por_sistema", "unica"}


def _empty(node):
    if isinstance(node, dict):
        return {k: _empty(v) for k, v in node.items()}
    if isinstance(node, list):
        return []
    return None


def template() -> dict:
    """Empty structure (null = unknown) with every schema key."""
    p = _empty(SCHEMA)
    p["schema_version"] = VERSION
    for r in p["detection_rules"].values():
        r["ifc_classes"] = []   # lists are never null
    return p


def template_with_examples() -> dict:
    """Template for analysis_template.json: lists with one empty item, as in the specification."""
    p = template()
    p["systems"] = [_empty(SCHEMA["systems"][0])]
    p["anomalies"] = [_empty(SCHEMA["anomalies"][0])]
    return copy.deepcopy(p)
