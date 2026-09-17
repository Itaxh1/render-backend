"""Explicit deploy-time maintenance entry point. Never connects to the database."""
from fastapi import FastAPI
from starlette.responses import JSONResponse

app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

@app.get('/readyz')
@app.get('/healthz')
def health():
    return {'status':'maintenance'}

@app.api_route('/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'])
def unavailable(path: str):
    return JSONResponse({'detail':'Maintenance in progress. Please retry shortly.'},status_code=503,
                        headers={'Retry-After':'30','Cache-Control':'no-store'})
