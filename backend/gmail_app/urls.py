from django.urls import path
from .views import ProfileView, UserRegistrationView, UserLoginView, UserLogoutView
from .views import GoogleAuthURLView, GoogleOAuthCallbackView, GoogleTokenStatusView, GoogleTokenRevokeView, GmailConnectivityTestView
from rest_framework_simplejwt.views import TokenRefreshView
from .views import GmailEmailListView, GmailEmailMetadataView, GmailSearchView, GmailLabelsView
from . import views

urlpatterns = [
    # JWT realted apis
    path("auth/register/",  UserRegistrationView.as_view(), name="user_register"),
    path('auth/login/', UserLoginView.as_view(), name = 'user_login'),
    path('auth/logout/', UserLogoutView.as_view(), name = 'user_logout'),
    path('auth/refresh/', TokenRefreshView.as_view(), name = 'token_refresh'),
    # URL to view profile of the user
    path('profile/', ProfileView.as_view(), name='user_profile'),   
    #OAuth related apis
    path('auth/google/url/', GoogleAuthURLView.as_view(), name='google_auth_url'),
    path('auth/google/callback/', GoogleOAuthCallbackView.as_view(), name='google_callback'),
    path('auth/google/status/', GoogleTokenStatusView.as_view(), name='google_token_status'),
    path('auth/google/revoke/', GoogleTokenRevokeView.as_view(), name='google_token_revoke'),

    #Gmail connectivity test apis
    path('gmail/connectivity/', GmailConnectivityTestView.as_view(), name='gmail_connectivity'),

    #Gmail operations related apis
    path('gmail/emails/', GmailEmailListView.as_view(), name='gmail_emails'),
    path('gmail/emails/metadata/', GmailEmailMetadataView.as_view(), name='gmail_email_metadata'),
    path('gmail/search/', GmailSearchView.as_view(), name='gmail_search'),
    path('gmail/labels/', GmailLabelsView.as_view(), name='gmail_labels'),

    #Email operations related apis
    path('gmail/emails/delete/<str:message_id>/', views.EmailDeleteView.as_view(), name='email_delete'),
    path('gmail/emails/recover/<str:message_id>/', views.EmailRecoverView.as_view(), name='email_recover'),
    path('gmail/emails/bulk-delete/', views.BulkEmailDeleteView.as_view(), name='bulk_email_delete'),
    path('gmail/emails/bulk-recover/', views.BulkEmailRecoverView.as_view(), name='bulk_email_recover'),
    path('tasks/<str:task_id>/', views.TaskStatusView.as_view(), name='task_status'),

    #Deletion?recovery bt query for testing
    path('gmail/delete-by-query/', views.DeleteByQueryView.as_view(), name='query_email_delete'),
    path('gmail/recover-by-query/', views.RecoverByQueryView.as_view(), name='query_email_recover'),


    #Advance operations -> Preview before deletion, Undo etc related Apis
    path('gmail/preview/', views.EmailPreviewView.as_view(), name='email_preview'),
    path('gmail/rules/', views.DeletionRulesView.as_view(), name='deletion_rules'),
    path('gmail/rules/<str:rule_id>/execute/', views.ExecuteRuleView.as_view(), name='execute_rule'),
    path('gmail/undo/', views.UndoOperationView.as_view(), name='undo_operations'),
    path('gmail/undo/<str:undo_id>/', views.UndoOperationView.as_view(), name='execute_undo'),
    path('gmail/stats/', views.EmailStatsView.as_view(), name='email_stats'),

    # New endpoint for Gmail email count
    path('gmail/count/', views.GmailEmailCountView.as_view(), name='gmail_email_count'),

]