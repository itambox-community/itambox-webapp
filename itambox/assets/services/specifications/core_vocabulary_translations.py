"""Shipped translations of the core vocabulary (issue #832).

Only vocabulary the application ships is translated here. Tenant-authored
labels are never touched. The lookup validates exact key coverage against the
live vocabulary before returning rows, so a new field, choice, section, help
text, or word-like unit without a German entry fails before seeding.

Entries listed in ``_UNCHANGED_CHOICE_KEYS`` are technical names (protocols, standards,
product marks) that are intentionally identical in every language; they still
get an explicit translation row so the drift check can be exact.
"""

from __future__ import annotations

from assets.services.specifications.core_vocabulary import get_core_vocabulary

LOCALE_DE = "de"

_SECTION_DESCRIPTION_DE = (
    "Wiederverwendbarer Funktionsbereich. Der Geltungsbereich hängt vom jeweiligen Feld und seinem Ziel ab, "
    "nicht von der Abschnittsbezeichnung."
)

_SECTIONS_DE = {
    "product-physical": "Produkt und physische Merkmale",
    "compute-memory": "Rechenleistung und Arbeitsspeicher",
    "virtual-compute": "Virtuelle Rechenleistung",
    "storage": "Datenspeicher",
    "connectivity-io": "Konnektivität und Schnittstellen",
    "network-function": "Netzwerkfunktion",
    "power-battery": "Stromversorgung und Batterie",
    "display-av-imaging": "Display, AV und Bildtechnik",
    "print-scan": "Drucken und Scannen",
    "management-security": "Verwaltung und Sicherheit",
    "environmental-ruggedization": "Umgebung und Robustheit",
    "sensors-control": "Sensorik und Steuerung",
    "compliance-sustainability": "Konformität und Nachhaltigkeit",
}

_FIELDS_DE = {
    "form_factor": ("Bauform", "Physische Bauform, keine Betriebsrolle oder Kategorie."),
    "rack_units": ("Rackhöhe", "Nominell belegte vertikale Rackhöhe des Produkts."),
    "weight": (
        "Produktgewicht",
        "Gewicht des unverpackten Produkts in dieser Konfiguration, nicht das Versandgewicht.",
    ),
    "ip_rating": (
        "Angegebene IP-Schutzart",
        "Vom Hersteller angegebener Textcode; die Syntaxprüfung ist keine Zertifizierungsprüfung.",
    ),
    "processor_model": (
        "Prozessormodell",
        "Modell des Hauptprozessors; am Typ die ausgelieferte Konfiguration, am Asset die beobachtete "
        "Konfiguration. Keine nummerierten Sockeleinträge.",
    ),
    "core_count": ("Physische Prozessorkerne", "Gesamtzahl physischer CPU-Kerne, weder Threads noch virtuelle CPUs."),
    "memory_capacity": (
        "Arbeitsspeicherkapazität",
        "Gesamter nutzbarer verbauter bzw. konfigurierter RAM in binären GiB; der Auslieferungswert am Asset-Typ "
        "und der tatsächliche Wert am Asset werden unabhängig voneinander erfasst.",
    ),
    "memory_type": (
        "Speichertechnologie",
        "Haupttechnologie des Arbeitsspeichers; bei einem unbekannten virtuellen Host keine Werte erfinden.",
    ),
    "vcpu_count": (
        "Virtuelle CPUs",
        "Zugewiesene virtuelle Prozessoren, nie aus der Zahl physischer Kerne abgeleitet.",
    ),
    "storage_capacity": (
        "Lokale Speicherkapazität",
        "Summe der Nennkapazitäten lokaler Blockgeräte vor Berücksichtigung des RAID- und Dateisystem-Overheads. "
        "Von entfernten Systemen eingebundener Speicher und Messwerte zum freien Speicherplatz bleiben "
        "unberücksichtigt. Es sind ausdrücklich binäre GiB zu verwenden.",
    ),
    "storage_medium": (
        "Primäres Speichermedium",
        "Kurzangabe des wichtigsten Speichermediums; bei gemischter Ausstattung hybrid verwenden. NVMe ist eine "
        "Schnittstelle, kein eigenes Medium.",
    ),
    "storage_interface": (
        "Primäre Speicherschnittstelle",
        "Schnittstelle der wichtigsten Speicherkonfiguration. Einzelne Laufwerke und Schnittstellen gehören nicht "
        "in die Spezifikationsfelder.",
    ),
    "drive_bay_count": (
        "Laufwerksschächte",
        "Gesamtzahl physischer Laufwerksschächte, keine Liste verbauter Laufwerke.",
    ),
    "hot_swap_supported": (
        "Hot-Swap für Laufwerke unterstützt",
        "Das Produkt unterstützt den Austausch von Laufwerken im laufenden Betrieb. Nicht angegeben bedeutet "
        "unbekannt; Nein bedeutet ausdrücklich nicht unterstützt.",
    ),
    "ethernet_port_count": (
        "Ethernet-Ports",
        "Gesamtzahl fest eingebauter Ethernet-Datenports, ohne separat installierte Erweiterungsmodule.",
    ),
    "ethernet_speeds": (
        "Unterstützte Ethernet-Geschwindigkeiten",
        "Menge der unterstützten nominalen Verbindungsraten, nicht ein Eintrag je physischem Port.",
    ),
    "wifi_standards": (
        "Unterstützte WLAN-Standards",
        "Beschreibt die vom Produkt unterstützten WLAN-Standards, nicht den ausgehandelten Verbindungszustand.",
    ),
    "usb_port_count": ("USB-Ports", "Fest eingebaute, von außen zugängliche USB-Anschlüsse."),
    "network_functions": (
        "Netzwerkfunktionen",
        "Unterstützte Produktfunktionen; die vom Betreiber zugewiesene Asset-Rolle wird dadurch nicht ersetzt.",
    ),
    "switching_capacity": (
        "Angegebene Switching-Kapazität",
        "Vom Hersteller angegebene aggregierte Switching-Kapazität, weder Firewall-Durchsatz noch aktueller "
        "Datenverkehr.",
    ),
    "poe_port_count": ("PoE-Ausgangsports", "Anzahl fest eingebauter Ports, die PoE bereitstellen können."),
    "poe_budget": (
        "Gesamtes PoE-Leistungsbudget",
        "Insgesamt verfügbare PoE-Ausgangsleistung für die angegebene Netzteilkonfiguration.",
    ),
    "input_voltage_min": (
        "Minimale Nenneingangsspannung",
        "Untere Grenze der unterstützten Nenneingangsspannung, bei Wechselstromgeräten als Effektivwert.",
    ),
    "input_voltage_max": (
        "Maximale Nenneingangsspannung",
        "Obere Grenze der unterstützten Nenneingangsspannung; muss mindestens dem Minimum entsprechen, wenn "
        "beide vorhanden sind.",
    ),
    "power_consumption_max": (
        "Maximale Nennleistungsaufnahme",
        "Maximale Nenneingangsleistung, nicht die Ausgangsleistung laut Netzteil-Typenschild.",
    ),
    "power_output": (
        "Nennwirkleistung am Ausgang",
        "Nennwirkleistung im Dauerbetrieb am Ausgang einer USV oder Stromquelle.",
    ),
    "apparent_power": (
        "Nennscheinleistung am Ausgang",
        "Nennscheinleistung am Ausgang. VA und W sind verschiedene Größen; eine implizite Umrechnung erfolgt nicht.",
    ),
    "battery_capacity": (
        "Energiekapazität der Batterie",
        "Nennenergie der Batterie in dieser Konfiguration, nicht der verbleibende Ladezustand in Prozent.",
    ),
    "battery_runtime": (
        "Batterielaufzeit",
        "Laufzeit bei der ausdrücklich erfassten Testlast der Batterie. "
        "Weder eine isolierte Laufzeitangabe noch ein Prozentwert werden gespeichert.",
    ),
    "battery_runtime_load": (
        "Testlast für die Batterielaufzeit",
        "Positive Wirklast für die erfasste Batterielaufzeit; erforderlich, sobald eine Batterielaufzeit gesetzt ist.",
    ),
    "display_size": (
        "Displaydiagonale",
        "Diagonale des Hauptdisplays; Anordnungen aus mehreren Displays hier nicht einzeln aufführen.",
    ),
    "display_resolution": (
        "Native Auflösung des Displays",
        "Native Auflösung im Format: Anzahl horizontaler Pixel x Anzahl vertikaler Pixel; das Trennzeichen ist "
        "ein ASCII-x, z. B. 1920x1080.",
    ),
    "touch_supported": ("Touch-Eingabe unterstützt", "Touchfunktion des Hauptdisplays."),
    "camera_resolution": (
        "Auflösung der Hauptkamera",
        "Nominelle Standbildauflösung der Hauptkamera.",
    ),
    "print_technology": ("Drucktechnologie", "Haupttechnologie des Druckwerks."),
    "color_supported": (
        "Farbdruck unterstützt",
        "Fähigkeit zum Farbdruck, nicht die aktuellen Druckeinstellungen.",
    ),
    "duplex_supported": (
        "Automatischer Duplexdruck unterstützt",
        "Fähigkeit zum automatischen beidseitigen Druck; manuelles Wenden des Papiers zählt nicht.",
    ),
    "print_speed": (
        "Angegebene Schwarzweiß-Druckgeschwindigkeit",
        "Vom Hersteller angegebene einseitige Schwarzweiß-Geschwindigkeit in A4/Letter im Basismodus. Andere "
        "Messbedingungen gehören in die Notizen, nicht in einen vergleichbaren Skalarwert.",
    ),
    "operating_system_family": (
        "Betriebssystemfamilie",
        "Ausgelieferte bzw. voreingestellte Betriebssystemfamilie am Typ gegenüber der unabhängig erfassten "
        "installierten Familie auf einem Gerät.",
    ),
    "management_protocols": (
        "Unterstützte Verwaltungsprotokolle",
        "Beschreibt ausschließlich die vom Produkt unterstützten Verwaltungsprotokolle. Ein aufgeführtes "
        "Protokoll ist weder eine "
        "Empfehlung noch ein Nachweis, dass es aktiviert ist.",
    ),
    "hostname": (
        "Hostname",
        "Konfigurierter Hostname ohne abschließenden Punkt, geprüft vom vorhandenen längenbegrenzten Validator "
        "für registrierte Hostnamen.",
    ),
    "firmware_version": (
        "Installierte Firmware-Version",
        "Beobachtete Firmware-Version dieses Assets, nie aus dem Produktkatalog abgeleitet.",
    ),
    "operating_temperature_min": (
        "Minimale Betriebstemperatur",
        "Vom Hersteller angegebene untere Grenze der Umgebungstemperatur im Betrieb.",
    ),
    "operating_temperature_max": (
        "Maximale Betriebstemperatur",
        "Vom Hersteller angegebene obere Grenze der Umgebungstemperatur, mindestens so hoch wie die untere "
        "Grenze, wenn beide vorhanden sind.",
    ),
    "outdoor_rated": (
        "Für den Außeneinsatz ausgelegt",
        "Ausdrückliche Herstellererklärung; nicht aus dem Text der IP-Schutzart abgeleitet.",
    ),
    "acoustic_level": (
        "Angegebener Geräuschpegel",
        "Angegebener A-bewerteter Schalldruck. Die Messbedingung gehört in die Beschreibung. Die Werte sind nicht "
        "automatisch zwischen Herstellern vergleichbar.",
    ),
    "sensor_types": (
        "Erfassbare Messgrößen",
        "Arten der erfassten Größen, keine Messwerte einzelner Sensorkanäle.",
    ),
    "analog_input_count": ("Analogeingänge", "Anzahl fest eingebauter analoger Eingangskanäle."),
    "digital_input_count": ("Digitaleingänge", "Anzahl fest eingebauter digitaler Eingangskanäle."),
    "industrial_protocols": (
        "Industrielle Protokolle",
        "Unterstützte Protokollfamilien, keine konfigurierten Verbindungen.",
    ),
    "regulatory_certifications": (
        "Angegebene Konformitätskennzeichnungen",
        "Ungeprüfte Produktangaben, kein Nachweis der Konformität einer Organisation oder eines abgeschlossenen Audits.",
    ),
    "rohs_compliant": (
        "RoHS-Konformität angegeben",
        "Nur Herstellererklärung; fehlend bedeutet unbekannt.",
    ),
    "energy_certifications": (
        "Angegebene Energie- und Nachhaltigkeitskennzeichen",
        "Ungeprüfte Produktangaben; die Anwendung leitet daraus keine rechtlich belastbare Aussage über eine "
        "Zertifizierung ab.",
    ),
    "country_of_origin": (
        "Angegebenes Herkunftsland",
        "Von der Quelle angegebener zweibuchstabiger Ländercode. Das Muster prüft nur die Syntax, nicht die "
        "Zugehörigkeit zu einem gepflegten Länderverzeichnis.",
    ),
}

# Word-like unit tokens only; symbolic units (kg, W, GiB, ...) are language neutral.
_UNITS_DE = {"pages_per_minute": "ppm"}

_CHOICE_SETS_DE = {
    "form-factor": (
        "Bauform",
        {
            "desktop": "Desktop",
            "notebook": "Notebook",
            "tablet": "Tablet",
            "phone": "Telefon",
            "rack": "Rack",
            "tower": "Tower",
            "blade": "Blade",
            "appliance": "Appliance",
            "peripheral": "Peripheriegerät",
            "module": "Modul",
            "other": "Sonstige",
        },
    ),
    "memory-type": ("Speichertyp", {"other": "Sonstige"}),
    "storage-medium": (
        "Speichermedium",
        {
            "hdd": "Festplatte",
            "ssd": "Solid-State-Laufwerk",
            "flash": "Eingebetteter Flash-Speicher",
            "optical": "Optisch",
            "tape": "Band",
            "hybrid": "Hybrid",
            "other": "Sonstige",
        },
    ),
    "storage-interface": ("Primäre Speicherschnittstelle", {"virtual": "Virtuell", "other": "Sonstige"}),
    "ethernet-speeds": ("Ethernet-Geschwindigkeiten", {"2_5g": "2,5 Gbit/s"}),
    "wifi-standards": ("WLAN-Standards", {}),
    "network-functions": (
        "Netzwerkfunktionen",
        {
            "switch": "Switch",
            "router": "Router",
            "firewall": "Firewall",
            "wlan_ap": "WLAN-Access-Point",
            "wlan_controller": "WLAN-Controller",
            "load_balancer": "Load-Balancer",
            "vpn_gateway": "VPN-Gateway",
            "modem": "Modem",
            "gateway": "Gateway",
            "bridge": "Bridge",
        },
    ),
    "print-technology": (
        "Drucktechnologie",
        {
            "laser": "Laser",
            "inkjet": "Tintenstrahl",
            "thermal": "Thermodruck",
            "dot_matrix": "Nadeldruck",
            "dye_sublimation": "Thermosublimation",
            "solid_ink": "Festtinte",
            "other": "Sonstige",
        },
    ),
    "operating-system-family": (
        "Betriebssystemfamilie",
        {
            "windows": "Windows",
            "windows_server": "Windows Server",
            "macos": "macOS",
            "linux": "Linux",
            "chromeos": "ChromeOS",
            "ios": "iOS",
            "ipados": "iPadOS",
            "android": "Android",
            "bsd": "BSD",
            "network_os": "Netzwerk-Betriebssystem",
            "embedded": "Eingebettetes Betriebssystem",
            "other": "Sonstige",
        },
    ),
    "management-protocols": ("Verwaltungsprotokolle", {"other": "Sonstige"}),
    "sensor-types": (
        "Sensortypen",
        {
            "temperature": "Temperatur",
            "humidity": "Luftfeuchtigkeit",
            "pressure": "Druck",
            "light": "Licht",
            "motion": "Bewegung",
            "vibration": "Vibration",
            "current": "Strom",
            "voltage": "Spannung",
            "power": "Leistung",
            "energy": "Energie",
            "flow": "Durchfluss",
            "level": "Füllstand",
            "gas": "Gas",
            "smoke": "Rauch",
            "position": "Position",
            "other": "Sonstige",
        },
    ),
    "industrial-protocols": ("Industrielle Protokolle", {"other": "Sonstige"}),
    "regulatory-certifications": ("Angegebene Konformitätskennzeichnungen", {"other": "Sonstige"}),
    "energy-certifications": ("Angegebene Energie- und Nachhaltigkeitskennzeichen", {"other": "Sonstige"}),
}


# Explicitly unchanged choice labels still need a German row. Keeping their
# immutable keys here makes a newly shipped choice fail the lockstep check.
_UNCHANGED_CHOICE_KEYS = {
    "memory-type": {"ddr3", "ddr4", "ddr5", "lpddr4x", "lpddr5", "lpddr5x", "hbm2", "hbm3"},
    "storage-interface": {"sata", "sas", "nvme", "usb", "emmc", "ufs"},
    "ethernet-speeds": {"10m", "100m", "1g", "5g", "10g", "25g", "40g", "50g", "100g", "200g", "400g"},
    "wifi-standards": {"802_11a", "802_11b", "802_11g", "802_11n", "802_11ac", "802_11ax", "802_11be"},
    "management-protocols": {
        "https",
        "ssh",
        "snmpv1",
        "snmpv2c",
        "snmpv3",
        "redfish",
        "ipmi",
        "wsman",
        "rest",
        "graphql",
    },
    "industrial-protocols": {
        "modbus_rtu",
        "modbus_tcp",
        "profinet",
        "profibus",
        "ethernet_ip",
        "ethercat",
        "bacnet_ip",
        "opc_ua",
        "canopen",
    },
    "regulatory-certifications": {"ce", "fcc", "ul", "csa", "ukca", "vcci", "rcm", "eac"},
    "energy-certifications": {
        "energy_star",
        "epeat_bronze",
        "epeat_silver",
        "epeat_gold",
        "80plus",
        "80plus_bronze",
        "80plus_silver",
        "80plus_gold",
        "80plus_platinum",
        "80plus_titanium",
    },
}


class CoreVocabularyTranslationDrift(RuntimeError):
    """Raised when shipped core vocabulary and its German rows diverge."""


def _assert_exact_keys(subject: str, expected: set[str], actual: set[str]) -> None:
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise CoreVocabularyTranslationDrift(
            f"German core vocabulary translations are out of lockstep for {subject}: "
            f"missing={missing}, unexpected={unexpected}"
        )


def _word_like_unit_tokens(vocabulary: dict) -> set[str]:
    fields = vocabulary["active_fields"] + vocabulary["reserved_retired_fields"]
    return {
        row["canonical_unit"]
        for row in fields
        if isinstance(row.get("canonical_unit"), str)
        and "_" in row["canonical_unit"]
        and row["canonical_unit"].isascii()
        and row["canonical_unit"].replace("_", "").isalnum()
    }


def _validate_translation_coverage(vocabulary: dict) -> None:
    fields = vocabulary["active_fields"] + vocabulary["reserved_retired_fields"]
    _assert_exact_keys("fields", {row["key"] for row in fields}, set(_FIELDS_DE))
    _assert_exact_keys("sections", {row["slug"] for row in vocabulary["sections"]}, set(_SECTIONS_DE))
    _assert_exact_keys("choice sets", {row["slug"] for row in vocabulary["choice_sets"]}, set(_CHOICE_SETS_DE))
    _assert_exact_keys("word-like unit tokens", _word_like_unit_tokens(vocabulary), set(_UNITS_DE))

    for choice_set in vocabulary["choice_sets"]:
        slug = choice_set["slug"]
        translated_keys = set(_CHOICE_SETS_DE[slug][1]) | _UNCHANGED_CHOICE_KEYS.get(slug, set())
        _assert_exact_keys(
            f"choices in itambox/{slug}",
            {choice["key"] for choice in choice_set["choices"]},
            translated_keys,
        )


def get_core_vocabulary_translations(locale: str = LOCALE_DE, *, vocabulary: dict | None = None) -> dict[str, dict]:
    """Return the complete explicit translation tables for ``locale``.

    Choices without an override are technical names (``DDR5``, ``HTTPS``,
    ``10 Gbit/s``, ...) and keep their canonical label verbatim. The result is
    keyed by definition identity (``namespace/key`` or ``namespace/slug``).
    """
    if locale != LOCALE_DE:
        raise KeyError(locale)
    vocabulary = vocabulary if vocabulary is not None else get_core_vocabulary()
    _validate_translation_coverage(vocabulary)
    fields = {}
    for row in vocabulary["active_fields"] + vocabulary["reserved_retired_fields"]:
        label, help_text = _FIELDS_DE[row["key"]]
        fields[row["identity"]] = {
            "label": label,
            "help_text": help_text,
            "unit_label": _UNITS_DE.get(row["canonical_unit"] or "", ""),
        }
    sections = {
        row["identity"]: {"label": _SECTIONS_DE[row["slug"]], "description": _SECTION_DESCRIPTION_DE}
        for row in vocabulary["sections"]
    }
    choice_sets = {}
    for row in vocabulary["choice_sets"]:
        set_label, overrides = _CHOICE_SETS_DE[row["slug"]]
        choice_sets[row["identity"]] = {
            "label": set_label,
            "choices": {
                choice["key"]: overrides.get(choice["key"], choice["label"])
                for choice in row["choices"]
                if choice["key"] in overrides or choice["key"] in _UNCHANGED_CHOICE_KEYS.get(row["slug"], set())
            },
        }
    return {"fields": fields, "sections": sections, "choice_sets": choice_sets}


def seed_core_vocabulary_translations(
    get_model,
    locale: str = LOCALE_DE,
    using: str = "default",
    *,
    vocabulary: dict | None = None,
) -> int:
    """Upsert the shipped translation rows for already-created core definitions.

    ``get_model`` is ``django.apps.apps.get_model`` at runtime and the
    historical ``apps.get_model`` inside a migration. Rows for identities that
    no longer exist are skipped; tenant-authored rows are never matched because
    only ``namespace='itambox'`` identities are looked up. Returns the number of
    rows written.
    """
    tables = get_core_vocabulary_translations(locale, vocabulary=vocabulary)
    field_model = get_model("extras", "CustomField")
    choice_model = get_model("extras", "CustomFieldChoice")
    set_model = get_model("extras", "CustomFieldChoiceSet")
    fieldset_model = get_model("extras", "CustomFieldset")
    field_tr = get_model("extras", "CustomFieldTranslation")
    choice_tr = get_model("extras", "CustomFieldChoiceTranslation")
    fieldset_tr = get_model("extras", "CustomFieldsetTranslation")
    written = 0
    for identity, values in tables["fields"].items():
        field = field_model.objects.using(using).filter(namespace="itambox", name=identity.rsplit("/", 1)[1]).first()
        if field is not None:
            field_tr.objects.using(using).update_or_create(custom_field=field, locale=locale, defaults=values)
            written += 1
    for identity, values in tables["sections"].items():
        fieldset = (
            fieldset_model.objects.using(using).filter(namespace="itambox", slug=identity.rsplit("/", 1)[1]).first()
        )
        if fieldset is not None:
            fieldset_tr.objects.using(using).update_or_create(fieldset=fieldset, locale=locale, defaults=values)
            written += 1
    for identity, values in tables["choice_sets"].items():
        choice_set = set_model.objects.using(using).filter(namespace="itambox", slug=identity.rsplit("/", 1)[1]).first()
        if choice_set is None:
            continue
        for choice in choice_model.objects.using(using).filter(choice_set=choice_set):
            label = values["choices"].get(choice.key)
            if label is not None:
                choice_tr.objects.using(using).update_or_create(choice=choice, locale=locale, defaults={"label": label})
                written += 1
    return written
