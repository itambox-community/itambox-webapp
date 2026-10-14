# Asset Type YAML Authoring

Use this guide to import a hardware model and its specifications into the current ITAMbox deployment. This is a specialised YAML import. The generic bulk importer also accepts CSV and YAML, but this page documents the Asset Type-specific fields and the nested `specs` mapping.

## Before you start

- You need an account with the asset-type import permission (`assets.add_assettype`) and provider catalogue access. Without it the import page and `can_import` refuse the actor.
- To let the import create a missing Manufacturer, you also need `assets.add_manufacturer`.
- Any `category` you supply must already exist in your deployment. The importer never creates a category.
- An `asset_role`, if you supply one, must already exist.
- The Manufacturer and Model you name must not already exist as an Asset Type: an existing pair is skipped, not updated.

## 1. Start with an example

The following values are illustrative only; they are not verified specifications for a real product. The importer accepts one YAML mapping per model, or a YAML list of mappings.

```yaml
- manufacturer: Example Vendor
  model: Example Laptop 14
  category: laptops
  specs:
    form_factor: notebook
    processor_model: Example CPU
    core_count: 8
    memory_capacity: "16 GiB"
    memory_type: ddr5
    storage_capacity: "512 GiB"
    storage_medium: ssd
    storage_interface: nvme
    display_size: "14 in"
    display_resolution: "1920x1080"
    weight: "1.4 kg"
```

A record needs `manufacturer` and `model`. Other supported top-level keys are `slug`, `part_number`, `ean`, `category`, `asset_role`, `description`, `comments`, and `specs`. Unknown top-level keys and unknown `specs` keys are rejected. Empty values may be omitted.

## 2. Choose valid specification keys and values

The built-in core vocabulary contains 54 active specification fields. The table below lists the keys this application revision accepts, with their value types or choices, units and fieldset memberships. It is derived from that vocabulary and from the generated schema `itambox/assets/services/specifications/generated_schema.json` at the revision this page documents; re-derive it after any vocabulary change. Use the key exactly as written; labels are for display and are not accepted as substitutes. Quantity values are strings with a space before the canonical unit, for example `16 GiB` or `14 in`; quote them in YAML. Multi-select values are lists. Choice values are lowercase stable keys.

`Targets` distinguishes model-level `asset_type` values from device-level `asset` observations. A field whose target is only `asset` describes an individual device and cannot be supplied as a model specification.

For the full generated JSON Schema, including numeric bounds, patterns, and accepted choices, run `python manage.py export_catalogue_schema --output <path>` in your deployment. The committed copy is `itambox/assets/services/specifications/generated_schema.json` in the source tree; it is not part of this documentation site. The community contribution rules (one file per model, required category, and filename/path conventions) are in the [type-library contributor guide](https://github.com/itambox-community/type-library/blob/fd93a8be10a910f363f27ce6f327a32d790678ea/CONTRIBUTING.md). That contributor guide is for submitting catalogue files; it is not the app import format or the REST API contract.

### All built-in specification fields

| Key | Label | Targets | YAML value/type | Validation | Fieldsets |
|---|---|---|---|---|---|
| `form_factor` | Form factor | asset_type | one of: `appliance`, `blade`, `desktop`, `module`, `notebook`, `other`, `peripheral`, `phone`, `rack`, `tablet`, `tower` | `{"max_values": 1}` | itambox/product-physical |
| `rack_units` | Rack height | asset_type | quoted quantity with space + `U` | `{"minimum": "0.0", "maximum": "100.0", "scale": 1}` | itambox/product-physical |
| `weight` | Product weight | asset_type | quoted quantity with space + `kg` | `{"minimum": "0.000", "maximum": "100000.000", "scale": 3}` | itambox/product-physical |
| `ip_rating` | Declared IP rating | asset_type | string | `{"max_length": 8, "regex": "^IP[0-6X][0-9X]K?$"}` | itambox/product-physical |
| `processor_model` | Processor model | asset_type, asset | string | `{"max_length": 255}` | itambox/compute-memory |
| `core_count` | Physical processor cores | asset_type, asset | integer | `{"minimum": "0", "maximum": "1048576"}` | itambox/compute-memory |
| `memory_capacity` | Memory capacity | asset_type, asset | quoted quantity with space + `GiB` | `{"minimum": "0.000", "maximum": "1048576.000", "scale": 3}` | itambox/compute-memory, itambox/virtual-compute |
| `memory_type` | Memory technology | asset_type, asset | one of: `ddr3`, `ddr4`, `ddr5`, `hbm2`, `hbm3`, `lpddr4x`, `lpddr5`, `lpddr5x`, `other` | `{"max_values": 1}` | itambox/compute-memory |
| `vcpu_count` | Virtual CPUs | asset_type, asset | integer | `{"minimum": "1", "maximum": "1048576"}` | itambox/virtual-compute |
| `storage_capacity` | Local storage capacity | asset_type, asset | quoted quantity with space + `GiB` | `{"minimum": "0.000", "maximum": "1073741824.000", "scale": 3}` | itambox/storage |
| `storage_medium` | Primary storage medium | asset_type, asset | one of: `flash`, `hdd`, `hybrid`, `optical`, `other`, `ssd`, `tape` | `{"max_values": 1}` | itambox/storage |
| `storage_interface` | Primary storage interface | asset_type, asset | one of: `emmc`, `nvme`, `other`, `sas`, `sata`, `ufs`, `usb`, `virtual` | `{"max_values": 1}` | itambox/storage |
| `drive_bay_count` | Drive bays | asset_type | integer | `{"minimum": "0", "maximum": "65535"}` | itambox/storage |
| `hot_swap_supported` | Hot-swap storage supported | asset_type | boolean | `{}` | itambox/storage |
| `ethernet_port_count` | Ethernet ports | asset_type | integer | `{"minimum": "0", "maximum": "65535"}` | itambox/connectivity-io |
| `ethernet_speeds` | Supported Ethernet speeds | asset_type | list of: `100g`, `100m`, `10g`, `10m`, `1g`, `200g`, `25g`, `2_5g`, `400g`, `40g`, `50g`, `5g` | `{"max_values": 16}` | itambox/connectivity-io |
| `wifi_standards` | Supported Wi-Fi standards | asset_type | list of: `802_11a`, `802_11ac`, `802_11ax`, `802_11b`, `802_11be`, `802_11g`, `802_11n` | `{"max_values": 8}` | itambox/connectivity-io |
| `usb_port_count` | USB ports | asset_type | integer | `{"minimum": "0", "maximum": "1024"}` | itambox/connectivity-io |
| `network_functions` | Network capabilities | asset_type | list of: `bridge`, `firewall`, `gateway`, `load_balancer`, `modem`, `router`, `switch`, `vpn_gateway`, `wlan_ap`, `wlan_controller` | `{"max_values": 10}` | itambox/network-function |
| `switching_capacity` | Rated switching capacity | asset_type | quoted quantity with space + `Gbit/s` | `{"minimum": "0.000", "maximum": "1000000000.000", "scale": 3}` | itambox/network-function |
| `poe_port_count` | PoE output ports | asset_type | integer | `{"minimum": "0", "maximum": "65535"}` | itambox/network-function |
| `poe_budget` | Total PoE output budget | asset_type | quoted quantity with space + `W` | `{"minimum": "0.000", "maximum": "10000000.000", "scale": 3}` | itambox/network-function |
| `input_voltage_min` | Minimum rated input voltage | asset_type | quoted quantity with space + `V` | `{"minimum": "0.000", "maximum": "1000000.000", "scale": 3}` | itambox/power-battery |
| `input_voltage_max` | Maximum rated input voltage | asset_type | quoted quantity with space + `V` | `{"minimum": "0.000", "maximum": "1000000.000", "scale": 3, "rule": "voltage_max_gte_min"}` | itambox/power-battery |
| `power_consumption_max` | Maximum rated power draw | asset_type | quoted quantity with space + `W` | `{"minimum": "0.000", "maximum": "100000000.000", "scale": 3}` | itambox/power-battery |
| `power_output` | Rated real output power | asset_type | quoted quantity with space + `W` | `{"minimum": "0.000", "maximum": "100000000.000", "scale": 3}` | itambox/power-battery |
| `apparent_power` | Rated apparent output power | asset_type | quoted quantity with space + `VA` | `{"minimum": "0.000", "maximum": "100000000.000", "scale": 3}` | itambox/power-battery |
| `battery_capacity` | Battery energy capacity | asset_type, asset | quoted quantity with space + `Wh` | `{"minimum": "0.000", "maximum": "10000000.000", "scale": 3}` | itambox/power-battery |
| `battery_runtime` | Battery runtime | asset_type, asset | quoted quantity with space + `min` | `{"minimum": "0", "maximum": "5256000"}` | itambox/power-battery |
| `battery_runtime_load` | Runtime test load | asset_type, asset | quoted quantity with space + `W` | `{"minimum": "0.001", "maximum": "100000000.000", "scale": 3, "rule": "runtime_requires_load"}` | itambox/power-battery |
| `display_size` | Display diagonal | asset_type | quoted quantity with space + `in` | `{"minimum": "0.00", "maximum": "1000.00", "scale": 2}` | itambox/display-av-imaging |
| `display_resolution` | Native display resolution | asset_type | string | `{"max_length": 11, "regex": "^[1-9][0-9]{1,4}x[1-9][0-9]{1,4}$"}` | itambox/display-av-imaging |
| `touch_supported` | Touch input supported | asset_type | boolean | `{}` | itambox/display-av-imaging |
| `camera_resolution` | Principal camera resolution | asset_type | quoted quantity with space + `MP` | `{"minimum": "0.00", "maximum": "10000.00", "scale": 2}` | itambox/display-av-imaging |
| `print_technology` | Print technology | asset_type | one of: `dot_matrix`, `dye_sublimation`, `inkjet`, `laser`, `other`, `solid_ink`, `thermal` | `{"max_values": 1}` | itambox/print-scan |
| `color_supported` | Color printing supported | asset_type | boolean | `{}` | itambox/print-scan |
| `duplex_supported` | Automatic duplex supported | asset_type | boolean | `{}` | itambox/print-scan |
| `print_speed` | Rated monochrome print speed | asset_type | quoted quantity with space + `pages_per_minute` | `{"minimum": "0.00", "maximum": "100000.00", "scale": 2}` | itambox/print-scan |
| `operating_system_family` | Operating system family | asset_type, asset | one of: `android`, `bsd`, `chromeos`, `embedded`, `ios`, `ipados`, `linux`, `macos`, `network_os`, `other`, `windows`, `windows_server` | `{"max_values": 1}` | itambox/management-security |
| `management_protocols` | Supported management protocols | asset_type | list of: `graphql`, `https`, `ipmi`, `other`, `redfish`, `rest`, `snmpv1`, `snmpv2c`, `snmpv3`, `ssh`, `wsman` | `{"max_values": 16}` | itambox/management-security |
| `hostname` | Hostname | asset | string | `{"max_length": 253, "rule": "rfc1123_hostname"}` | itambox/management-security |
| `firmware_version` | Installed firmware version | asset | string | `{"max_length": 255}` | itambox/management-security |
| `operating_temperature_min` | Minimum operating temperature | asset_type | quoted quantity with space + `°C` | `{"minimum": "-273.15", "maximum": "1000.00", "scale": 2}` | itambox/environmental-ruggedization |
| `operating_temperature_max` | Maximum operating temperature | asset_type | quoted quantity with space + `°C` | `{"minimum": "-273.15", "maximum": "1000.00", "scale": 2, "rule": "temperature_max_gte_min"}` | itambox/environmental-ruggedization |
| `outdoor_rated` | Outdoor use rated | asset_type | boolean | `{}` | itambox/environmental-ruggedization |
| `acoustic_level` | Declared acoustic level | asset_type | quoted quantity with space + `dBA` | `{"minimum": "0.00", "maximum": "250.00", "scale": 2}` | itambox/environmental-ruggedization |
| `sensor_types` | Sensor capabilities | asset_type | list of: `current`, `energy`, `flow`, `gas`, `humidity`, `level`, `light`, `motion`, `other`, `position`, `power`, `pressure`, `smoke`, `temperature`, `vibration`, `voltage` | `{"max_values": 32}` | itambox/sensors-control |
| `analog_input_count` | Analog inputs | asset_type | integer | `{"minimum": "0", "maximum": "65535"}` | itambox/sensors-control |
| `digital_input_count` | Digital inputs | asset_type | integer | `{"minimum": "0", "maximum": "65535"}` | itambox/sensors-control |
| `industrial_protocols` | Industrial protocols | asset_type | list of: `bacnet_ip`, `canopen`, `ethercat`, `ethernet_ip`, `modbus_rtu`, `modbus_tcp`, `opc_ua`, `other`, `profibus`, `profinet` | `{"max_values": 16}` | itambox/sensors-control |
| `regulatory_certifications` | Declared regulatory marks | asset_type | list of: `ce`, `csa`, `eac`, `fcc`, `other`, `rcm`, `ukca`, `ul`, `vcci` | `{"max_values": 16}` | itambox/compliance-sustainability |
| `rohs_compliant` | RoHS compliance declared | asset_type | boolean | `{}` | itambox/compliance-sustainability |
| `energy_certifications` | Declared energy and sustainability marks | asset_type | list of: `80plus`, `80plus_bronze`, `80plus_gold`, `80plus_platinum`, `80plus_silver`, `80plus_titanium`, `energy_star`, `epeat_bronze`, `epeat_gold`, `epeat_silver`, `other` | `{"max_values": 16}` | itambox/compliance-sustainability |
| `country_of_origin` | Declared country of origin | asset_type | string | `{"max_length": 2, "regex": "^[A-Z]{2}$"}` | itambox/compliance-sustainability |

## 3. Use a supported category

The built-in seed ships these category slugs as defaults:

`access-points`, `desktops`, `firewalls`, `laptops`, `mobile-phones`, `monitors`, `printers`, `routers`, `servers`, `switches`, `tablets`, `ups`, `virtual-machines`.

Any category that already exists in your deployment is accepted, including locally created ones; the built-in slugs above are only the common defaults. The importer never creates a category. With a category, its current default specification sections are used. If you omit the category and provide `specs`, sections are derived from the fields submitted. Review the resulting Type composition after import.

## 4. Import and review

1. Open the Asset Type import page at `/import/assets/assettype/`. Page access itself already requires the permissions listed under [Before you start](#before-you-start); a missing permission stops the request before any preview is shown.
2. Paste or upload YAML, then review the preview. It validates the schema and previews the Type creation before starting a job. An unknown category, unsupported key, malformed quantity, or a row that would need a Manufacturer you may not create is reported during preview, before confirmation.
3. If the named Manufacturer does not exist, it can be created only when the actor also has `assets.add_manufacturer`. An existing Manufacturer is matched by exact name, case-insensitive name, or slug. An `asset_role`, if supplied, must match an existing role by slug or case-insensitive name.
4. Confirm the import, then inspect the resulting job for row outcomes. An existing Asset Type for the same Manufacturer and case-insensitive Model name is **skipped**, not updated. Correct an existing Type through its edit workflow; re-importing is not an update mechanism.
5. Open the imported Type and review its selected specification sections and model-level values. Section composition controls which fields are available. See [Asset Types](../models/assets/assettype.md) and [Custom Fields](../usage/custom-fields.md) for editing fields, sections, and per-asset observations.

The import passes through the application's own model-level validation and composition rules. A schema-valid file is not proof that a database import succeeded: the category/default composition, the permissions above, and current application validation all still apply.

## Do not confuse these formats

- **Community catalogue files:** the separate type-library repository follows its own repository conventions and additionally requires `category` for every entry. Its contributor guide, not this page, governs submissions there.
- **App Asset Type import:** uses the supported keys above, allows an omitted category, performs a preview, and skips existing Manufacturer/Model pairs. Use `/import/assets/assettype/`.
- **Specification API writes:** `specification_patch` and the composition-preview contract documented in [Custom Fields](../usage/custom-fields.md) are the JSON write path. They do not accept these YAML catalogue files.
- **Custom fields and fieldsets:** define and compose fields; they do not expand the core `specs` YAML allowlist. To author local fields and add them to a Type, follow the [Custom Fields guide](../usage/custom-fields.md).

## Common errors

| Error | Correction |
|---|---|
| `memory_capacity: 16` | Write a quantity string with the required unit, for example `memory_capacity: "16 GiB"`. |
| `memory_capacity: "16 GB"` | Use the canonical unit `GiB`. The importer does not convert alternative units. |
| `memory_type: DDR5` | Choice keys are case-sensitive; use `ddr5`. |
| `ram: 16` | `ram` is not a core key. Choose an allowed `specs` key from the table. |
| A valid key is rejected during preview | Check its `Targets`; an `asset`-only field is for an individual device, not its Asset Type. Check the Type's active sections and required values too. |
| Existing Type is reported as skipped | Imports do not update existing Manufacturer/Model pairs. Edit the Type directly. |
| Category is unknown | Use an existing category slug from the list; category creation is not part of this import. |

The schema is generated from the application's core vocabulary by `manage.py export_catalogue_schema`, which writes the committed `generated_schema.json` by default and accepts `--output`. The field table above is a rendered view of that schema and is kept in step with it; treat the generated schema as the authority and re-render the table rather than editing either copy by hand.
