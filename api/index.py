from asgiref.wsgi import WsgiToAsgi
from app import app

asgi_app = WsgiToAsgi(app)

# Vercel appelle cette fonction
async def handler(request, context):
    return await asgi_app(request, context)
