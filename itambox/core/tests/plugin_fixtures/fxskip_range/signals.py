from django.db.models.signals import post_migrate

post_migrate.connect(lambda **kwargs: None, weak=False, dispatch_uid="fxskip_range_import_time")
