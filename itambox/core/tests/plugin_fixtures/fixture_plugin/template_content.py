from itambox.plugins.views import PluginTemplateContent


class FixtureManufacturerContent(PluginTemplateContent):
    models = ["assets.manufacturer"]

    def right_panel(self):
        return "fixture-plugin-panel-marker"


template_extensions = [FixtureManufacturerContent]
