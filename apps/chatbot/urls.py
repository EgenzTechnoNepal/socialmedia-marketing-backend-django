from django.urls import path

from . import teams, transfers, views

urlpatterns = [
    path("chatbot/settings", views.chatbot_settings),
    path("chatbot/keywords/<uuid:rule_id>", views.keyword_detail),
    path("chatbot/keywords", views.keywords_collection),
    path("chatbot/flows/<uuid:flow_id>", views.flow_detail),
    path("chatbot/flows", views.flows_collection),
    path("chatbot/ai-contexts/<uuid:ctx_id>", views.ai_context_detail),
    path("chatbot/ai-contexts", views.ai_contexts_collection),
    path("chatbot/ai/generate", views.generate_ai),
    path("chatbot/sessions/<uuid:session_id>", views.session_detail),
    path("chatbot/sessions", views.sessions_collection),
    path("chatbot/transfers/pick", transfers.pick_next_transfer),
    path("chatbot/transfers/<uuid:transfer_id>/resume", transfers.resume_transfer),
    path("chatbot/transfers/<uuid:transfer_id>/assign", transfers.assign_transfer),
    path("chatbot/transfers", transfers.transfers_collection),
    path("teams/<uuid:team_id>/members/<uuid:member_user_id>", teams.team_member_detail),
    path("teams/<uuid:team_id>/members", teams.team_members),
    path("teams/<uuid:team_id>", teams.team_detail),
    path("teams", teams.teams_collection),
]
