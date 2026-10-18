# Testing a Plugin

`itambox.plugins.testing` verifies a plugin against a real ITAMbox start. It
has one entry point, `verify_plugins`, and is part of the Stable plugin API.

The helper boots a fresh interpreter with your plugins configured, exactly as an
operator lists them in `ITAMBOX_PLUGINS`, and drives the real startup phases:
resolve, compose, ready and assemble. No database connection is opened. A fresh
interpreter is required because plugins are loaded at settings time and cannot be
swapped inside a running test process.

## What it checks

- startup completes and none of the requested plugins was skipped (a plugin
  outside the supported `itambox_range` fails the check instead of passing quietly);
- the manifest is complete (`description`, `itambox_range`);
- every declared resource loads and is registered under the plugin that declared it;
- the UI URLconf (`urls.py`) is mounted and every REST prefix resolves;
- the composed GraphQL schema builds.

A failure raises `PluginVerificationError`, an `AssertionError` whose message names
the plugin and the phase, for example
`Plugin "example_plugin" failed during compose: TypeError: ...`. The exception also
carries `plugin`, `phase` and `detail` attributes.

## Example

Copy this into `tests/test_startup.py` of your plugin repository and run it with
the ITAMbox test environment active:

```python
from pathlib import Path

from django.test import SimpleTestCase

from itambox.plugins.testing import verify_plugins

PLUGIN_ROOT = Path(__file__).resolve().parents[1]  # the directory that contains example_plugin/


class ExamplePluginStartupTests(SimpleTestCase):
    def test_plugin_starts_and_registers(self):
        report = verify_plugins("example_plugin", pythonpath=[PLUGIN_ROOT])
        self.assertEqual(report.active, ("example_plugin",))
        self.assertIn("viewsets", report.plugins["example_plugin"]["registered"])
```

## Signature

`verify_plugins(*plugins, pythonpath=(), environ=None, timeout=180)`

| Argument | Meaning |
|---|---|
| `plugins` | One or more importable package names. Several plugins start together. |
| `pythonpath` | Directories that contain the plugin packages. |
| `environ` | Extra environment variables for the start, for example operator configuration overrides. |
| `timeout` | Seconds before the start is abandoned. |

It returns a `PluginReport` with `active` (tuple of names), `skipped` and
`plugins` (per plugin: `version`, `itambox_range`, `registered` resource kinds,
`ui`, `api_prefixes`).
