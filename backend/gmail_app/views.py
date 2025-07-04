from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import generics

from .serializers import UserRegistrationSerializer, UserSerializer, UserLoginSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework_simplejwt.exceptions import TokenError

# Importing OAuth related things
from django.contrib.auth.models import User
from django.shortcuts import redirect
from .utils import generate_auth_url, exchange_code_for_tokens, create_gmail_service, revoke_user_tokens
from .models import GoogleOAuthToken
from .serializers import GoogleAuthURLSerializer, GoogleOAuthSerializer

from django.conf import settings
import importlib
from datetime import timedelta, datetime

from .gmail_operations import GmailOperations, build_search_query

from celery.result import AsyncResult
from .email_operations import EmailDeletionManager, bulk_delete_emails_task, bulk_recover_emails_task, recover_by_query_task, delete_by_query_task

# Adding logger for enchanced debugging
import logging
logger = logging.getLogger(__name__)

# ****************************************Login/Register related Views*********************************

class UserLoginView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = UserLoginSerializer(data = request.data)
        if serializer.is_valid():
            user = serializer.validated_data['user']
            refresh = RefreshToken.for_user(user)

            return Response({
                'message' : 'Login Succesful',
                'user' : UserSerializer(user).data,
                'tokens' : {
                    'refresh' : str(refresh),
                    'access' : str(refresh.access_token)
                }
            }, status=status.HTTP_200_OK)
        
        return Response(serializer.errors, status = status.HTTP_400_BAD_REQUEST)
    

class UserLogoutView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        try:
            refresh_token = request.data.get('refresh')
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
                
            return Response({
                'message': 'Logout successful'
            }, status=status.HTTP_200_OK)
        except TokenError:
            return Response({
                'error': 'Invalid token'
            }, status=status.HTTP_400_BAD_REQUEST)
        
    
class UserRegistrationView(generics.CreateAPIView):
    serializer_class = UserRegistrationSerializer
    permission_classes = [AllowAny]
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data = request.data)
        serializer.is_valid(raise_exception = True)
        user = serializer.save()

        #Generatng jwt token so that user wouldnt have to login after registering
        refresh = RefreshToken.for_user(user)

        return Response({
            'message' : 'User created successfully',
            'user' : UserSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
        }, status=status.HTTP_201_CREATED)


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    
    def get_object(self):
        return self.request.user
    

# **********************************************Creating OAuth related views********************************************
class GoogleAuthURLView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Generate Google OAuth2 authorization URL with enhanced error handling"""
        try:
            auth_url, state = generate_auth_url(request.user.id)

            if not auth_url:
                logger.error(f"Failed to generate auth URL for user {request.user.username}")
                return Response({
                    'error': 'Failed to generate authorization URL',
                    'success': False
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            logger.info(f"Generated auth URL for user {request.user.username}")
            return Response({
                'auth_url': auth_url,
                'state': state,
                'message': 'Visit the auth_url to authorize Gmail access',
                'success': True
            })
        
        except Exception as e:
            logger.error(f"Auth URL generation error for user {request.user.username}: {e}")
            return Response({
                'error': f'Authorization setup failed: {str(e)}',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



class GoogleOAuthCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        """Handle Google OAuth2 callback and redirect to frontend"""
        code = request.GET.get('code')
        state = request.GET.get('state')
        error = request.GET.get('error')
        
        # Get frontend URL from settings
        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')

        if error:
            logger.warning(f"OAuth authorization denied: {error}")
            return redirect(f"{frontend_url}/dashboard?oauth=error&message={error}")
        
        if not state or not code:
            logger.warning("OAuth callback missing required parameters")
            return redirect(f"{frontend_url}/dashboard?oauth=error&message=missing_parameters")
        
        try:
            # Validate user from state
            try:
                user = User.objects.get(id=int(state))
            except (User.DoesNotExist, ValueError):
                logger.error(f"Invalid state parameter: {state}")
                return redirect(f"{frontend_url}/dashboard?oauth=error&message=invalid_state")
        
            # Manual token exchange with enhanced error handling
            try:
                token_response = exchange_code_for_tokens(code)
            except Exception as e:
                logger.error(f"Token exchange failed for user {user.username}: {e}")
                return redirect(f"{frontend_url}/dashboard?oauth=error&message=token_exchange_failed")

            # Validate required tokens
            if 'access_token' not in token_response:
                logger.error(f"No access token received for user {user.username}")
                return redirect(f"{frontend_url}/dashboard?oauth=error&message=invalid_token_response")

            # Get granted scopes from URL parameter
            granted_scopes_param = request.GET.get('scope', '')
            granted_scopes = granted_scopes_param.split() if granted_scopes_param else []

            # Validate essential scopes
            required_scopes = ['https://www.googleapis.com/auth/gmail.modify']
            missing_scopes = [scope for scope in required_scopes if scope not in granted_scopes]
            
            if missing_scopes:
                logger.warning(f"Missing required scopes for user {user.username}: {missing_scopes}")
                return redirect(f"{frontend_url}/dashboard?oauth=error&message=missing_scopes")

            # Calculate expiry with timezone awareness
            expiry = None
            if 'expires_in' in token_response:
                from django.utils import timezone
                expiry = timezone.now() + timedelta(seconds=token_response['expires_in'])

            # Save tokens to database
            token, created = GoogleOAuthToken.objects.update_or_create(
                user=user,
                defaults={
                    'access_token': token_response['access_token'],
                    'refresh_token': token_response.get('refresh_token'),
                    'token_uri': 'https://oauth2.googleapis.com/token',
                    'client_id': settings.GOOGLE_OAUTH2_CLIENT_ID,
                    'client_secret': settings.GOOGLE_OAUTH2_CLIENT_SECRET,
                    'scopes': granted_scopes,
                    'expiry': expiry
                }
            )

            # Test Gmail API connection
            gmail_address = 'Unknown'
            try:
                gmail_service = create_gmail_service(user)
                if gmail_service:
                    profile = gmail_service.users().getProfile(userId='me').execute()
                    gmail_address = profile.get('emailAddress', 'Unknown')
                
            except Exception as e:
                logger.error(f"Gmail API test failed for user {user.username}: {e}")
                # Don't fail the whole process, just continue

            logger.info(f"OAuth setup successful for user {user.username}, Gmail: {gmail_address}")
            
            # Redirect to frontend with success
            return redirect(f"{frontend_url}/dashboard?oauth=success&email={gmail_address}")
        
        except Exception as e:
            logger.error(f"OAuth callback error for user state {state}: {e}")
            return redirect(f"{frontend_url}/dashboard?oauth=error&message=server_error")

class GoogleTokenStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Check Google OAuth token status with Gmail connectivity test"""
        try:
            token = GoogleOAuthToken.objects.get(user=request.user)
            
            # Test Gmail connectivity
            try:
                gmail_service = create_gmail_service(request.user)
                is_connected = gmail_service is not None
                
                if is_connected:
                    # Get basic Gmail info
                    profile = gmail_service.users().getProfile(userId='me').execute()
                    gmail_info = {
                        'email_address': profile.get('emailAddress'),
                        'messages_total': profile.get('messagesTotal', 0),
                        'threads_total': profile.get('threadsTotal', 0)
                    }
                else:
                    gmail_info = None
                    
            except Exception as e:
                logger.warning(f"Gmail connectivity test failed for user {request.user.username}: {e}")
                is_connected = False
                gmail_info = None

            return Response({
                'has_token': True,
                'is_expired': token.is_expired(),
                'is_connected': is_connected,
                'scopes': token.scopes,
                'created_at': token.created_at,
                'updated_at': token.updated_at,
                'gmail_info': gmail_info
            })
            
        except GoogleOAuthToken.DoesNotExist:
            return Response({
                'has_token': False,
                'is_expired': None,
                'is_connected': False,
                'scopes': [],
                'message': 'No Gmail authorization found. Please authorize first.',
                'gmail_info': None
            })


class GoogleTokenRevokeView(APIView):
    permission_classes = [IsAuthenticated]
    
    def delete(self, request):
        """Revoke Google OAuth tokens with enhanced error handling"""
        try:
            success = revoke_user_tokens(request.user)
            
            if success:
                logger.info(f"OAuth tokens revoked for user {request.user.username}")
                return Response({
                    'message': 'Gmail authorization revoked successfully',
                    'success': True
                })
            else:
                logger.error(f"Token revocation failed for user {request.user.username}")
                return Response({
                    'error': 'Failed to revoke authorization completely',
                    'success': False,
                    'message': 'Local tokens removed but Google revocation may have failed'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
        except Exception as e:
            logger.error(f"Token revocation error for user {request.user.username}: {e}")
            return Response({
                'error': f'Revocation failed: {str(e)}',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# *******************************************Gmail Connectivity Test Views*******************************************


from .gmail_utils import test_gmail_connectivity, GmailServiceManager

class GmailConnectivityTestView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Test Gmail API connectivity and return detailed status"""
        try:
            connectivity_result = test_gmail_connectivity(request.user)
            
            if connectivity_result['connected']:
                return Response({
                    'status': 'success',
                    'connected': True,
                    'gmail_profile': connectivity_result['profile'],
                    'message': 'Gmail API connection successful'
                })
            else:
                return Response({
                    'status': 'error',
                    'connected': False,
                    'error': connectivity_result['error'],
                    'message': 'Gmail API connection failed'
                }, status=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            logger.error(f"Gmail connectivity test error for user {request.user.username}: {e}")
            return Response({
                'status': 'error',
                'connected': False,
                'error': str(e),
                'message': 'Connectivity test failed'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def post(self, request):
        """Force refresh Gmail connection"""
        try:
            manager = GmailServiceManager(request.user)
            service = manager.get_service(force_refresh=True)
            
            if service:
                connectivity_result = test_gmail_connectivity(request.user)
                return Response({
                    'status': 'success',
                    'connected': True,
                    'gmail_profile': connectivity_result['profile'],
                    'message': 'Gmail connection refreshed successfully'
                })
            else:
                return Response({
                    'status': 'error',
                    'connected': False,
                    'error': manager.get_last_error(),
                    'message': 'Failed to refresh Gmail connection'
                }, status=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            return Response({
                'status': 'error',
                'connected': False,
                'error': str(e),
                'message': 'Connection refresh failed'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        

class GmailEmailListView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """List emails with pagination"""
        try:
            page_size = int(request.GET.get('page_size', 20))
            page_token = request.GET.get('page_token')
            label_ids = request.GET.getlist('label_ids', [])
            
            gmail_ops = GmailOperations(request.user)
            
            # Build query
            query_parts = []
            if label_ids:
                for label_id in label_ids:
                    query_parts.append(f'label:{label_id}')
            
            query = ' '.join(query_parts) if query_parts else ''
            
            result = gmail_ops.search_emails(
                query=query,
                max_results=page_size,
                page_token=page_token
            )
            
            if 'error' in result:
                return Response({
                    'status': 'error',
                    'error': result['error']
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Return same structure as search
            return Response({
                'results': result.get('messages', []),
                'count': result.get('resultSizeEstimate', 0),
                'next': result.get('nextPageToken'),
                'previous': None
            })
            
        except Exception as e:
            logger.error(f"List emails error for user {request.user.username}: {e}")
            return Response({
                'status': 'error',
                'error': f'Failed to list emails: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class GmailEmailMetadataView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Get metadata for specific emails"""
        try:
            message_ids = request.data.get('message_ids', [])
            
            if not message_ids:
                return Response({
                    'error': 'message_ids required'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if len(message_ids) > 1000:
                return Response({
                    'error': 'Too many message IDs (max 1000)'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            gmail_ops = GmailOperations(request.user)
            result = gmail_ops.get_email_metadata(message_ids)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result,
                'count': len(result['emails'])
            })
            
        except Exception as e:
            logger.error(f"Email metadata error for user {request.user.username}: {e}")
            return Response({
                'error': 'Failed to get email metadata',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class GmailSearchView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Search emails with Gmail query syntax"""
        try:
            search_query = request.GET.get('q', '')
            page_size = int(request.GET.get('page_size', 20))
            page_token = request.GET.get('page_token')
            
            if not search_query.strip():
                return Response({
                    'status': 'error',
                    'error': 'Search query (q) parameter is required'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            gmail_ops = GmailOperations(request.user)
            
            # Get emails matching the search query
            result = gmail_ops.search_emails(
                query=search_query,
                max_results=page_size,
                page_token=page_token
            )
            
            if 'error' in result:
                return Response({
                    'status': 'error',
                    'error': result['error']
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Process email data for frontend
            processed_emails = []
            for email in result.get('messages', []):
                processed_emails.append({
                    'id': email.get('id'),
                    'threadId': email.get('threadId'),
                    'labelIds': email.get('labelIds', []),
                    'snippet': email.get('snippet', ''),
                    'from': email.get('from', 'Unknown'),
                    'to': email.get('to', 'Unknown'),
                    'subject': email.get('subject', 'No Subject'),
                    'date': email.get('date', 'Unknown'),
                    'size': email.get('sizeEstimate', 0),
                    'labels': email.get('labelIds', [])
                })
            
            # FIXED: Return structure that matches frontend expectations
            return Response({
                'results': processed_emails,  # Frontend expects 'results'
                'count': result.get('resultSizeEstimate', 0),  # Frontend expects 'count'
                'next': result.get('nextPageToken'),
                'previous': None,
                'query': search_query
            })
            
        except Exception as e:
            logger.error(f"Search error for user {request.user.username}: {e}")
            return Response({
                'status': 'error',
                'error': f'Search failed: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def post(self, request):
        """Advanced search using filter parameters"""
        try:
            filters = request.data
            
            # Build query from filters
            query = build_search_query(filters)
            max_results = filters.get('max_results', 100)
            
            if not query:
                return Response({
                    'error': 'No valid search filters provided'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            gmail_ops = GmailOperations(request.user)
            result = gmail_ops.search_emails(query, max_results)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result,
                'filters_used': filters,
                'generated_query': query
            })
            
        except Exception as e:
            logger.error(f"Advanced search error for user {request.user.username}: {e}")
            return Response({
                'error': 'Advanced search failed',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class GmailLabelsView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get all Gmail labels"""
        try:
            gmail_ops = GmailOperations(request.user)
            result = gmail_ops.get_labels()
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result
            })
            
        except Exception as e:
            logger.error(f"Labels error for user {request.user.username}: {e}")
            return Response({
                'error': 'Failed to get labels',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        


# ****************************Email Operations, deletion and recovery, single and bulk*********************************

class EmailDeleteView(APIView):
    permission_classes = [IsAuthenticated]
    
    def delete(self, request, message_id):
        """Delete a single email"""
        try:
            permanent = request.data.get('permanent', False)
            
            deletion_manager = EmailDeletionManager(request.user)
            result = deletion_manager.delete_single_email(message_id, permanent)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result
            })
            
        except Exception as e:
            logger.error(f"Single delete error for user {request.user.username}: {e}")
            return Response({
                'error': 'Delete operation failed',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class EmailRecoverView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request, message_id):
        """Recover a single email from trash"""
        try:
            deletion_manager = EmailDeletionManager(request.user)
            result = deletion_manager.recover_email(message_id)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result
            })
            
        except Exception as e:
            logger.error(f"Single recover error for user {request.user.username}: {e}")
            return Response({
                'error': 'Recover operation failed',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class BulkEmailDeleteView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Start bulk email deletion task"""
        try:
            message_ids = request.data.get('message_ids', [])
            permanent = request.data.get('permanent', False)
            batch_size = request.data.get('batch_size', 100)
            
            if not message_ids:
                return Response({
                    'error': 'message_ids required'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if len(message_ids) > 10000:
                return Response({
                    'error': 'Too many emails (max 10,000 per operation)'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Start Celery task
            task = bulk_delete_emails_task.delay(
                user_id=request.user.id,
                message_ids=message_ids,
                permanent=permanent,
                batch_size=min(batch_size, 500)
            )
            
            return Response({
                'status': 'started',
                'task_id': task.id,
                'total_emails': len(message_ids),
                'permanent': permanent,
                'message': 'Bulk deletion started. Use task_id to check progress.'
            })
            
        except Exception as e:
            logger.error(f"Bulk delete start error for user {request.user.username}: {e}")
            return Response({
                'error': 'Failed to start bulk deletion',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class BulkEmailRecoverView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Start bulk email recovery task"""
        try:
            message_ids = request.data.get('message_ids', [])
            batch_size = request.data.get('batch_size', 100)
            
            if not message_ids:
                return Response({
                    'error': 'message_ids required'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if len(message_ids) > 10000:
                return Response({
                    'error': 'Too many emails (max 10,000 per operation)'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Start Celery task
            task = bulk_recover_emails_task.delay(
                user_id=request.user.id,
                message_ids=message_ids,
                batch_size=min(batch_size, 500)
            )
            
            return Response({
                'status': 'started',
                'task_id': task.id,
                'total_emails': len(message_ids),
                'message': 'Bulk recovery started. Use task_id to check progress.'
            })
            
        except Exception as e:
            logger.error(f"Bulk recover start error for user {request.user.username}: {e}")
            return Response({
                'error': 'Failed to start bulk recovery',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class TaskStatusView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request, task_id):
        """Get status of a Celery task"""
        try:
            result = AsyncResult(task_id)
            
            if result.state == 'PENDING':
                return Response({
                    'task_id': task_id,
                    'status': 'PENDING',
                    'progress': {
                        'current': 0,
                        'total': 0,
                        'message': 'Task is waiting to start'
                    }
                })
            elif result.state == 'PROGRESS':
                return Response({
                    'task_id': task_id,
                    'status': 'PROGRESS',
                    'progress': {
                        'current': result.info.get('current', 0),
                        'total': result.info.get('total', 0),
                        'message': result.info.get('message', 'Processing...')
                    }
                })
            elif result.state == 'SUCCESS':
                return Response({
                    'task_id': task_id,
                    'status': 'SUCCESS',
                    'result': result.result,
                    'progress': {
                        'current': result.result.get('total', 0),
                        'total': result.result.get('total', 0),
                        'message': 'Completed successfully'
                    }
                })
            else:  # FAILURE
                return Response({
                    'task_id': task_id,
                    'status': 'FAILURE',
                    'result': {
                        'error': str(result.info) if result.info else 'Unknown error'
                    }
                })
                
        except Exception as e:
            logger.error(f"Task status error: {e}")
            return Response({
                'error': 'Failed to get task status',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        

# ******************Views to perform deletion/recvover with a search query for testing************************************

class DeleteByQueryView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Delete emails by search query with user-specified count"""
        try:
            search_query = request.data.get('q', '')
            max_emails = request.data.get('max_emails', 1000)  # User specifies this
            permanent = request.data.get('permanent', False)
            
            if not search_query:
                return Response({
                    'error': 'q parameter required'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Validate max_emails
            if max_emails > 10000:
                return Response({
                    'error': 'Maximum 10,000 emails per operation'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Start task
            task = delete_by_query_task.delay(
                user_id=request.user.id,
                search_query=search_query,
                max_emails=max_emails,
                permanent=permanent
            )
            
            return Response({
                'status': 'started',
                'task_id': task.id,
                'search_query': search_query,
                'max_emails': max_emails,
                'permanent': permanent,
                'message': f'Deletion started for up to {max_emails} emails. Use task_id to check progress.'
            })
            
        except Exception as e:
            return Response({
                'error': 'Failed to start deletion by query',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class RecoverByQueryView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Recover emails by search query with user-specified count"""
        try:
            search_query = request.data.get('q', '')
            max_emails = request.data.get('max_emails', 1000)
            
            if not search_query:
                return Response({
                    'error': 'q parameter required'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if max_emails > 10000:
                return Response({
                    'error': 'Maximum 10,000 emails per operation'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Start task
            task = recover_by_query_task.delay(
                user_id=request.user.id,
                search_query=search_query,
                max_emails=max_emails
            )
            
            return Response({
                'status': 'started',
                'task_id': task.id,
                'search_query': search_query,
                'max_emails': max_emails,
                'message': f'Recovery started for up to {max_emails} emails. Use task_id to check progress.'
            })
            
        except Exception as e:
            return Response({
                'error': 'Failed to start recovery by query',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        


# ******************************Advanced operations views********************************************
from .advanced_operations import EmailPreviewManager, SmartDeletionRules, UndoManager

class EmailPreviewView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Preview emails before bulk deletion"""
        try:
            # FIXED: Use 'q' to match frontend and other APIs
            search_query = request.data.get('q', '')  # Changed from 'search_query'
            sample_size = request.data.get('max_results', 20)  # Changed from 'sample_size'
            
            if not search_query:
                return Response({
                    'error': 'q parameter required'  # Updated error message
                }, status=status.HTTP_400_BAD_REQUEST)
            
            preview_manager = EmailPreviewManager(request.user)
            result = preview_manager.preview_deletion_query(search_query, sample_size)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            # FIXED: Transform response to match frontend expectations
            return Response({
                'status': 'success',
                'emails': result.get('preview_emails', []),  # Changed from 'preview_emails'
                'total_estimate': result.get('total_count', 0),  # Changed from 'total_count'
                'sample_count': result.get('sample_size', 0),
                'estimated_deletion_time': f"{result.get('estimated_storage_mb', 0)} MB"
            })
            
        except Exception as e:
            logger.error(f"Preview error for user {request.user.username}: {e}")
            return Response({
                'error': 'Preview failed',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class DeletionRulesView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get user's deletion rules"""
        try:
            rules_manager = SmartDeletionRules(request.user)
            rules = rules_manager.get_user_rules()
            
            return Response({
                'status': 'success',
                'rules': rules
            })
            
        except Exception as e:
            return Response({
                'error': 'Failed to get rules',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def post(self, request):
        """Create a new deletion rule"""
        try:
            rule_config = request.data
            
            rules_manager = SmartDeletionRules(request.user)
            result = rules_manager.create_deletion_rule(rule_config)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result
            })
            
        except Exception as e:
            return Response({
                'error': 'Failed to create rule',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ExecuteRuleView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request, rule_id):
        """Execute a specific deletion rule"""
        try:
            rules_manager = SmartDeletionRules(request.user)
            result = rules_manager.execute_rule(rule_id)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result
            })
            
        except Exception as e:
            return Response({
                'error': 'Failed to execute rule',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class UndoOperationView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get available undo points"""
        try:
            undo_manager = UndoManager(request.user)
            undo_points = undo_manager.get_undo_history()
            
            return Response({
                'status': 'success',
                'undo_points': undo_points
            })
            
        except Exception as e:
            return Response({
                'error': 'Failed to get undo history',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def post(self, request, undo_id):
        """Execute undo operation"""
        try:
            undo_manager = UndoManager(request.user)
            result = undo_manager.execute_undo(undo_id)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'data': result,
                'message': 'Undo operation completed successfully'
            })
            
        except Exception as e:
            return Response({
                'error': 'Undo operation failed',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class EmailStatsView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get email deletion statistics"""
        try:
            days_back = int(request.GET.get('days_back', 30))
            
            preview_manager = EmailPreviewManager(request.user)
            stats = preview_manager.get_deletion_statistics(days_back)
            
            if 'error' in stats:
                return Response(stats, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'status': 'success',
                'stats': stats,
                'period_days': days_back
            })
            
        except Exception as e:
            return Response({
                'error': 'Failed to get statistics',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class GmailEmailCountView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get accurate email count for a query"""
        try:
            query = request.GET.get('q', '')
            
            if not query.strip():
                return Response({
                    'error': 'Query (q) parameter is required'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            gmail_ops = GmailOperations(request.user)
            
            # Use Gmail's quick estimate for better UX
            result = gmail_ops.get_quick_email_estimate(query)
            
            if 'error' in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'count': result['count'],
                'is_estimate': result.get('is_estimate', True),
                'query': query
            })
            
        except Exception as e:
            logger.error(f"Count API error for user {request.user.username}: {e}")
            return Response({
                'error': 'Failed to count emails',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)