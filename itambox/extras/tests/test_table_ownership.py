from django.test import SimpleTestCase

from extras.models import EventRule, ExportTemplate, LabelTemplate, WebhookEndpoint
from extras.tables import EventRuleTable, ExportTemplateTable, LabelTemplateTable, WebhookEndpointTable


class ExtrasTableOwnershipTests(SimpleTestCase):
    def test_concrete_table_meta_contracts(self):
        contracts = (
            (
                ExportTemplateTable,
                ExportTemplate,
                ("name", "content_type", "file_extension", "mime_type"),
            ),
            (
                WebhookEndpointTable,
                WebhookEndpoint,
                ("name", "url", "http_method", "enabled", "retry_count"),
            ),
            (
                EventRuleTable,
                EventRule,
                ("name", "model", "action_type", "conditions", "enabled"),
            ),
            (
                LabelTemplateTable,
                LabelTemplate,
                ("name", "description", "content_types", "page_width", "page_height", "barcode_format"),
            ),
        )

        for table_class, model, fields in contracts:
            with self.subTest(table=table_class.__name__):
                self.assertEqual(table_class.__module__, "extras.tables")
                self.assertIs(table_class.Meta.model, model)
                self.assertEqual(table_class.Meta.fields, fields)
                self.assertEqual(table_class.Meta.sequence, fields)
