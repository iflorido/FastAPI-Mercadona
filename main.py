import sqlite3
import asyncio
from pathlib import Path
import random
import traceback
import httpx
import unicodedata
import re
import uuid  # Importado una sola vez arriba
from typing import List, Optional

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from pydantic import BaseModel

# --- CONFIGURACIÓN ---
DB_FILE = Path("mercadona.db")
MERCADONA_API_URL = "https://tienda.mercadona.es/api/categories/"

# --- MODELOS PYDANTIC (Esquemas de datos) ---

class SubCategory(BaseModel):
    id: int
    name: str

class MainCategory(BaseModel):
    id: int
    name: str
    categories: List[SubCategory]

class ApiResponse(BaseModel):
    results: List[MainCategory]

class PriceInstructions(BaseModel):
    unit_price: Optional[str] = None
    bulk_price: Optional[str] = None
    unit_size: Optional[float] = None
    size_format: Optional[str] = None

class Product(BaseModel):
    id: str
    display_name: str
    thumbnail: str
    price_instructions: PriceInstructions
    share_url: str

class SubCategoryWithProducts(BaseModel):
    id: int
    name: str
    products: Optional[List[Product]] = [] 

class Photo(BaseModel):
    regular: str
    
class Supplier(BaseModel):
    name: str

class Details(BaseModel):
    brand: Optional[str] = None
    origin: Optional[str] = None
    suppliers: List[Supplier]
    legal_name: Optional[str] = None
    mandatory_mentions: Optional[str] = None
    description: Optional[str] = None
    storage_instructions: Optional[str] = None
    
class NutritionInformation(BaseModel):
    allergens: Optional[str] = None
    ingredients: Optional[str] = None

class ProductDetail(BaseModel):
    id: str
    ean: str
    display_name: str
    thumbnail: Optional[str] = None
    brand: Optional[str] = None
    photos: List[Photo]
    details: Details
    packaging: Optional[str] = None
    price_instructions: PriceInstructions
    nutrition_information: NutritionInformation
    share_url: Optional[str] = None

class CategoryDetail(BaseModel):
    id: int
    name: str
    categories: List[SubCategoryWithProducts]
    
# Modelos simplificados para la fase 1 de sincronización
class SubCategorySimple(BaseModel):
    id: int
    name: str

class MainCategorySimple(BaseModel):
    id: int
    name: str
    categories: List[SubCategorySimple]
    
class ApiResponseSimple(BaseModel):
    results: List[MainCategorySimple]


# --- INICIALIZACIÓN DE FASTAPI ---

app = FastAPI()

# 1. Archivos Estáticos (CSS, Imágenes, JS)
# Esto permite usar url_for('static', path='...')
app.mount("/static", StaticFiles(directory="static"), name="static")

# 2. Middlewares
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"]) 
app.add_middleware(SessionMiddleware, secret_key="una_clave_muy_secreta_y_aleatoria")

# 3. CORS (Para permitir acceso desde tu App React Native)
origins = ["*"] 
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. Templates (Jinja2)
templates = Jinja2Templates(directory="templates")

# --- FUNCIONES AUXILIARES ---

def slugify(text: str) -> str:
    """Convierte texto a slug URL-friendly."""
    if not text:
        return ""
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    return text.strip('-')

templates.env.filters["slugify"] = slugify

def parse_price(price_str):
    """Convierte '2,50 €' a float 2.50"""
    if not price_str:
        return 0.0
    try:
        clean_str = price_str.replace('€', '').replace(',', '.').strip()
        return float(clean_str)
    except ValueError:
        return 0.0

def get_cart_data(request: Request):
    """
    Recupera carrito de sesión y lo enriquece con datos de DB.
    Crucial para el Checkout y Success.
    """
    cart = request.session.get("cart", {})
    cart_items = []
    total_price = 0.0
    
    if not cart:
        return [], 0.0

    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        for product_id, quantity in cart.items():
            cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
            product = cursor.fetchone()
            
            if product:
                price_float = parse_price(product["unit_price"])
                subtotal = price_float * quantity
                total_price += subtotal
                
                cart_items.append({
                    "id": product["id"],
                    "display_name": product["display_name"],
                    "name": product["display_name"],
                    "thumbnail": product["thumbnail"],
                    "price": price_float, # Float para Analytics
                    "unit_price": product["unit_price"], # String original para mostrar
                    "quantity": quantity,
                    "subtotal": f"{subtotal:.2f} €" # String formateado para vista
                })
    finally:
        conn.close()

    return cart_items, total_price

# --- LÓGICA DE BASE DE DATOS Y SINCRONIZACIÓN ---

def create_database_and_table():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id TEXT PRIMARY KEY,
        ean TEXT,
        display_name TEXT,
        thumbnail TEXT,
        unit_price TEXT,
        share_url TEXT
    )
    """)
    conn.commit()
    conn.close()

async def sync_database():
    try:
        create_database_and_table() 
        print("--- INICIANDO SINCRONIZACIÓN ---", flush=True)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
            'Accept-Language': 'es-ES,es;q=0.9',
        }
        semaphore = asyncio.Semaphore(3) 

        async with httpx.AsyncClient(headers=headers, timeout=40.0) as client:
            # FASE 1: Estructura
            print("Fase 1: Descargando estructura...", flush=True)
            response = await client.get(MERCADONA_API_URL)
            response.raise_for_status()
            api_data = ApiResponseSimple(**response.json())

            all_subcategory_ids = [
                sub_cat.id 
                for main_cat in api_data.results 
                for sub_cat in main_cat.categories
            ]
            
            async def fetch_cat(cat_id):
                async with semaphore:
                    await asyncio.sleep(random.uniform(0.3, 1.0)) 
                    try:
                        res = await client.get(f"https://tienda.mercadona.es/api/categories/{cat_id}")
                        res.raise_for_status()
                        cat_data = CategoryDetail(**res.json())
                        products_in_cat = []
                        for sub in cat_data.categories:
                            if sub.products:
                                products_in_cat.extend(sub.products)
                        return products_in_cat
                    except Exception:
                        return []

            tasks_f1 = [fetch_cat(cid) for cid in all_subcategory_ids]
            list_of_lists = await asyncio.gather(*tasks_f1)
            all_products_stubs = [p for sublist in list_of_lists for p in sublist]
            unique_ids = list({p.id for p in all_products_stubs})
            
            print(f"✔ Fase 1: {len(unique_ids)} productos únicos.", flush=True)
            await asyncio.sleep(2)

            # FASE 2: Detalles
            print(f"Fase 2: Obteniendo detalles...", flush=True)
            
            async def fetch_details(pid):
                async with semaphore:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    try:
                        res = await client.get(f"https://tienda.mercadona.es/api/products/{pid}")
                        if res.status_code == 404: return None
                        res.raise_for_status()
                        return ProductDetail(**res.json())
                    except Exception as e:
                        # print(f"Error {pid}: {e}")
                        return None

            tasks_f2 = [fetch_details(pid) for pid in unique_ids]
            detailed_products = await asyncio.gather(*tasks_f2)
            valid_products = [p for p in detailed_products if p is not None]

            # FASE 3: Guardar
            print(f"Fase 3: Guardando en SQL...", flush=True)
            conn = sqlite3.connect(DB_FILE, timeout=60)
            cursor = conn.cursor()
            data_tuples = [
                (p.id, p.ean, p.display_name, p.thumbnail, p.price_instructions.unit_price, p.share_url)
                for p in valid_products
            ]
            cursor.executemany("""
            INSERT OR REPLACE INTO products (id, ean, display_name, thumbnail, unit_price, share_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """, data_tuples)
            conn.commit()
            conn.close()
            print("✅ SINCRONIZACIÓN FINALIZADA.")

    except Exception:
        traceback.print_exc()

# --- EVENTOS STARTUP ---

@app.on_event("startup")
async def startup_event():
    create_database_and_table()
    if not DB_FILE.exists() or DB_FILE.stat().st_size == 0:
        # Ejecutamos la sincronización en segundo plano para no bloquear el arranque
        asyncio.create_task(sync_database())

# --- ENDPOINTS WEB ---

@app.get("/", response_class=HTMLResponse)
async def get_all_categories(request: Request): 
    try:
        cart = request.session.get("cart", {})
        cart_count = sum(cart.values())
        
        async with httpx.AsyncClient() as client:
            response = await client.get(MERCADONA_API_URL)
            response.raise_for_status() 
        
        data = response.json()
        api_data = ApiResponse(**data)
        
        return templates.TemplateResponse("index.html", {
            "request": request,
            "main_categories": api_data.results,
            "cart_count": cart_count
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/categories/{category_path}", response_class=HTMLResponse)
async def read_category(request: Request, category_path: str):
    try:
        category_id = int(category_path.split('-')[0])
    except ValueError:
        raise HTTPException(status_code=404, detail="ID inválido")
    
    cart = request.session.get("cart", {})
    cart_count = sum(cart.values())
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://tienda.mercadona.es/api/categories/{category_id}")
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Categoría no encontrada")
            
            data = response.json()
            category_data = CategoryDetail(**data)
            
            return templates.TemplateResponse("categoria.html", {
                "request": request,
                "category": category_data,
                "cart_count": cart_count
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/products/{product_path}", response_class=HTMLResponse)
async def read_product(request: Request, product_path: str):
    product_id = product_path.split('-')[0]
    cart = request.session.get("cart", {})
    cart_count = sum(cart.values())
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://tienda.mercadona.es/api/products/{product_id}")
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Producto no encontrado")
            
            data = response.json()
            product_data = ProductDetail(**data)
            
            return templates.TemplateResponse("productos.html", {
                "request": request,
                "product": product_data,
                "cart_count": cart_count
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/buscar", response_class=HTMLResponse)
async def search_products(request: Request, query: str):
    cart = request.session.get("cart", {})
    cart_count = sum(cart.values())
    
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    search_words = query.strip().split()
    results = []

    if search_words:
        name_conditions = " AND ".join(["display_name LIKE ?"] * len(search_words))
        name_params = [f"%{word}%" for word in search_words]
        
        sql_query = f"""
        SELECT * FROM products 
        WHERE ({name_conditions}) OR ean = ? OR id = ?
        """
        params = name_params + [query, query]
        
        cursor.execute(sql_query, params)
        results = cursor.fetchall()
    
    conn.close()

    return templates.TemplateResponse("resultados.html", {
        "request": request,
        "query": query,
        "results": results,
        "cart_count": cart_count
    })

# --- ENDPOINTS CARRITO & CHECKOUT ---

@app.post("/cart/add")
async def add_to_cart(request: Request, product_id: str = Form(...), quantity: int = Form(...)):
    cart = request.session.get("cart", {})
    if product_id in cart:
        cart[product_id] += quantity
    else:
        cart[product_id] = quantity
    request.session["cart"] = cart
    return RedirectResponse(url="/carrito", status_code=303)

@app.post("/cart/update")
async def update_cart(request: Request, product_id: str = Form(...), quantity: int = Form(...), action: str = Form(...)):
    cart = request.session.get("cart", {})
    if action == "delete":
        if product_id in cart: del cart[product_id]
    elif action == "update":
        if product_id in cart:
            if quantity > 0: cart[product_id] = quantity
            else: del cart[product_id]
                
    request.session["cart"] = cart
    return RedirectResponse(url="/carrito", status_code=303)

@app.get("/carrito", response_class=HTMLResponse)
async def view_cart(request: Request):
    cart_items, total_price = get_cart_data(request)
    return templates.TemplateResponse("carrito.html", {
        "request": request,
        "cart_items": cart_items,
        "total_price": f"{total_price:.2f}"
    })
    
@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request):
    cart_items, total_price = get_cart_data(request)
    if not cart_items:
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse("checkout.html", {
        "request": request,
        "cart_items": cart_items,
        "total": total_price 
    })
    
@app.post("/success", response_class=HTMLResponse)
async def success_page(request: Request):
    cart_items, total_price = get_cart_data(request)
    
    if not cart_items:
         return RedirectResponse(url="/", status_code=303)

    transaction_id = str(uuid.uuid4()).split('-')[0].upper()
    request.session.pop("cart", None) # Vaciar carrito
    
    return templates.TemplateResponse("success.html", {
        "request": request,
        "cart_items": cart_items,
        "total": total_price,
        "transaction_id": transaction_id,
        "shipping": 5.99
    })

# --- ENDPOINTS API JSON (Para App Móvil) ---

@app.get("/api/v1/categories")
async def get_json_categories():
    async with httpx.AsyncClient() as client:
        response = await client.get(MERCADONA_API_URL)
        return response.json()

@app.get("/api/v1/products/{product_id}")
async def get_product_details_json(product_id: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://tienda.mercadona.es/api/products/{product_id}")
        return response.json()

@app.get("/api/v1/categories/{category_id}")
async def get_category_products_json(category_id: int):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://tienda.mercadona.es/api/categories/{category_id}")
        return response.json()

@app.get("/api/v1/buscar")
async def search_products_api(query: str):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    search_words = query.strip().split()
    results = []
    if search_words:
        name_conditions = " AND ".join(["display_name LIKE ?"] * len(search_words))
        name_params = [f"%{word}%" for word in search_words]
        sql_query = f"SELECT * FROM products WHERE ({name_conditions}) OR ean = ? OR id = ?"
        params = name_params + [query, query]
        cursor.execute(sql_query, params)
        results = cursor.fetchall()
    
    conn.close()
    return results

# --- SITEMAP & TOOLS ---

@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request):
    base_url = str(request.base_url).rstrip("/")
    urls = [f"{base_url}/", f"{base_url}/buscar"]

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(MERCADONA_API_URL, timeout=5.0)
            if response.status_code == 200:
                for category in response.json().get("results", []):
                    cat_slug = slugify(category['name'])
                    urls.append(f"{base_url}/categories/{category['id']}-{cat_slug}")
                    for subcat in category.get("categories", []):
                         sub_slug = slugify(subcat['name'])
                         urls.append(f"{base_url}/categories/{subcat['id']}-{sub_slug}")
    except Exception: pass

    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT id, display_name FROM products")
        for pid, name in cursor.fetchall():
            urls.append(f"{base_url}/products/{pid}-{slugify(name)}")
        conn.close()
    except Exception: pass

    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        xml_content += f'  <url>\n    <loc>{url}</loc>\n    <changefreq>daily</changefreq>\n  </url>\n'
    xml_content += '</urlset>'

    return Response(content=xml_content, media_type="application/xml")

@app.get("/actualizar-db")
async def update_db_endpoint(background_tasks: BackgroundTasks):
    background_tasks.add_task(sync_database)
    return RedirectResponse(url="/", status_code=303)