from django.urls import path

from apps.contacts import views as contacts_views
from apps.contacts import import_export
from apps.messaging import views as messaging_views

urlpatterns = [
    path("export/<str:table>/config", import_export.export_config),
    path("import/<str:table>/config", import_export.import_config),
    path("export", import_export.export_data),
    path("import", import_export.import_data),
    path("contacts/<uuid:contact_id>/messages", messaging_views.contact_messages),
    path("contacts/<uuid:contact_id>/mark-read", messaging_views.mark_contact_read),
    path("contacts/<uuid:contact_id>/notes/<uuid:note_id>", contacts_views.note_detail),
    path("contacts/<uuid:contact_id>/notes", contacts_views.notes_collection),
    path("contacts/<uuid:contact_id>/assign", contacts_views.assign_contact),
    path("contacts/<uuid:contact_id>/tags", contacts_views.update_contact_tags),
    path("contacts/<uuid:contact_id>/session-data", contacts_views.contact_session_data),
    path("contacts/<uuid:contact_id>", contacts_views.contact_detail),
    path("contacts", contacts_views.contacts_collection),
    path("tags/<str:name>", contacts_views.tag_detail),
    path("tags", contacts_views.tags_collection),
    path("messages/template", messaging_views.send_template),
    path("messages/media", messaging_views.send_media),
    path("media/<uuid:message_id>", messaging_views.serve_media),
    path("templates/<uuid:template_id>", messaging_views.template_detail),
    path("templates", messaging_views.templates_collection),
    path("canned-responses/<uuid:canned_id>/use", messaging_views.canned_use),
    path("canned-responses/<uuid:canned_id>", messaging_views.canned_detail),
    path("canned-responses", messaging_views.canned_collection),
]
