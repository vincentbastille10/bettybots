from app import app as _app

def handler(request, context):
    return _app(request.environ, start_response=None)
