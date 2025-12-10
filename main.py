import sqlite3
import asyncio
from pathlib import Path
import random
import traceback
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from typing import List, Optional
import httpx
import sys
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi import Form
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware # para que funcione bien detrás de un proxy como Nginx con HTTPS.
from fastapi import FastAPI, Response # para manejar respuestas personalizadas y crear sitemaps
# --- Aquí vamos a definir los modelos para incluir los datos en el Sql ---


# --- Definimos la variable para luego usar esta db ---
DB_FILE = Path("mercadona.db")

# Modelo para las subcategorias
class SubCategory(BaseModel):
    id: int
    name: str
# Modelo para las categorias
class MainCategory(BaseModel):
    id: int
    name: str
    categories: List[SubCategory]

# Modelo para las respuestas de la API
class ApiResponse(BaseModel):
    results: List[MainCategory]
    
# Modelo para los datos de precios de un producto
class PriceInstructions(BaseModel):
    unit_price: Optional[str] = None
    bulk_price: Optional[str] = None
    unit_size: Optional[float] = None
    size_format: Optional[str] = None

# Modelo para un producto 
class Product(BaseModel):
    id: str
    display_name: str
    thumbnail: str
    price_instructions: PriceInstructions
    share_url: str

# Modelo para las subcategorías que ahora contienen una lista de productos
class SubCategoryWithProducts(BaseModel):
    id: int
    name: str
    products: Optional[List[Product]] = [] 

# Modelo para las fotos del producto
class Photo(BaseModel):
    regular: str
    
# Modelo para los proveedores
class Supplier(BaseModel):
    name: str

# Modelo para el objeto anidado "details"
class Details(BaseModel):
    brand: Optional[str] = None # <-- CORRECCIÓN
    origin: Optional[str] = None
    suppliers: List[Supplier]
    legal_name: Optional[str] = None # <-- CORRECCIÓN
    mandatory_mentions: Optional[str] = None
    description: Optional[str] = None
    storage_instructions: Optional[str] = None
    
# Modelo para el objeto anidado "nutrition_information"
class NutritionInformation(BaseModel):
    allergens: Optional[str] = None
    ingredients: Optional[str] = None

# Modelo principal y completo para la página de un producto
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
    share_url: Optional[str] = None # <-- CORRECCIÓN

# Modelo principal para la respuesta de la API de una categoría específica
class CategoryDetail(BaseModel):
    id: int
    name: str
    # La respuesta contiene una lista de subcategorías con productos
    categories: List[SubCategoryWithProducts]
    
# Modelo para la estructura inicial de categorías
class SubCategorySimple(BaseModel):
    id: int
    name: str

class MainCategorySimple(BaseModel):
    id: int
    name: str
    categories: List[SubCategorySimple]
    
class ApiResponseSimple(BaseModel):
    results: List[MainCategorySimple]

# --- Despues de definir todas las clases para la base de datos comenzamos con la aplicación FastAPI ---

app = FastAPI()
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"]) # para que funcione bien detrás de un proxy como Nginx con HTTPS.
app.add_middleware(SessionMiddleware, secret_key="una_clave_muy_secreta_y_aleatoria")

origins = [
    "*" # Permite todas las fuentes, ideal para desarrollo.
    # Cuando lancemos la app en producción, deberíamos restringir esto a dominios específicos.
    # Añadieremos la url de donde lo alojaremos.
    # "http://localhost",
    # "http://localhost:8081",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"], # Permite todos los métodos (GET, POST, etc.)
    allow_headers=["*"], # Permite todas las cabeceras
)
# Definimos la carpeta 'templates'
templates = Jinja2Templates(directory="templates")

# De aquí vamos a sacar toda la información de API de Mercadona
MERCADONA_API_URL = "https://tienda.mercadona.es/api/categories/"

def create_database_and_table():
    """Crea la BD y la tabla de productos con la nueva columna EAN."""
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
        print("--- INICIANDO SINCRONIZACIÓN (Background Task) ---", flush=True)
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'}
        semaphore = asyncio.Semaphore(5) # Mantenemos 5 para ser amigables

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            
            # --- FASE 1: OBTENER TODOS LOS IDS DE PRODUCTO ÚNICOS --- #
            print("Fase 1: Descargando estructura de categorías...", flush=True)
            
            # 1. Obtener categorías principales
            response = await client.get("https://tienda.mercadona.es/api/categories/")
            response.raise_for_status()
            api_data = ApiResponseSimple(**response.json())
            
            # 2. Extraer IDs de subcategorías
            all_subcategory_ids = [
                sub_cat.id 
                for main_cat in api_data.results 
                for sub_cat in main_cat.categories
            ]
            
            total_cats = len(all_subcategory_ids)
            print(f"-> Se han encontrado {total_cats} subcategorías. Procesando...", flush=True)

            # Variable para contar progreso (mutable list hack o clase simple)
            progress = {"count": 0}

            async def fetch_product_list_from_category_with_progress(cat_id):
                async with semaphore:
                    # Reduje el sleep un poco para que sea más ágil, pero seguro
                    await asyncio.sleep(random.uniform(0.2, 0.8)) 
                    try:
                        res = await client.get(f"https://tienda.mercadona.es/api/categories/{cat_id}")
                        res.raise_for_status()
                        
                        cat_data = CategoryDetail(**res.json())
                        products_in_cat = []
                        for sub in cat_data.categories:
                            if sub.products:
                                products_in_cat.extend(sub.products)
                        
                        # LOG DE PROGRESO
                        progress["count"] += 1
                        if progress["count"] % 10 == 0: # Imprimir cada 10 para no saturar
                            print(f"   Fase 1: Procesadas {progress['count']}/{total_cats} categorías...", flush=True)
                            
                        return products_in_cat
                    except Exception as e:
                        print(f"   [Error] Categoría {cat_id}: {e}", flush=True)
                        return []

            # Ejecutamos tareas
            tasks_f1 = [fetch_product_list_from_category_with_progress(cid) for cid in all_subcategory_ids]
            list_of_lists = await asyncio.gather(*tasks_f1)
            
            # Aplanar
            all_products_stubs = [p for sublist in list_of_lists for p in sublist]
            # IDs únicos
            unique_ids = list({p.id for p in all_products_stubs})
            
            print(f"✔ Fase 1 Completada: {len(unique_ids)} productos únicos encontrados.", flush=True)

            
            # --- FASE 2: OBTENER DETALLES (ESTA ES LA PARTE LENTA) --- #
            total_prods = len(unique_ids)
            print(f"Fase 2: Obteniendo detalles de {total_prods} productos (esto tardará un rato)...", flush=True)
            
            progress["count"] = 0 # Reiniciar contador

            async def fetch_details_with_progress(pid):
                async with semaphore:
                    await asyncio.sleep(random.uniform(0.1, 0.5))
                    try:
                        res = await client.get(f"https://tienda.mercadona.es/api/products/{pid}")
                        if res.status_code == 404:
                            return None # Producto ya no existe
                        res.raise_for_status()
                        
                        # LOG DE PROGRESO
                        progress["count"] += 1
                        if progress["count"] % 50 == 0: # Imprimir cada 50 productos
                            print(f"   Fase 2: Descargados {progress['count']}/{total_prods} productos...", flush=True)
                            
                        return ProductDetail(**res.json())
                    except Exception as e:
                        # Errores puntuales no deben detener el proceso
                        # print(f"Error prod {pid}: {e}") 
                        return None

            tasks_f2 = [fetch_details_with_progress(pid) for pid in unique_ids]
            detailed_products = await asyncio.gather(*tasks_f2)
            valid_products = [p for p in detailed_products if p is not None]

            # --- FASE 3: GUARDAR --- #
            print(f"Fase 3: Guardando {len(valid_products)} registros en SQLite...", flush=True)
            
            conn = sqlite3.connect(DB_FILE, timeout=30) # Timeout más alto para escritura masiva
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
            
            print(f"✅ SINCRONIZACIÓN FINALIZADA EXITOSAMENTE. {len(valid_products)} productos actualizados.", flush=True)

    except Exception as e:
        print("\n❌ ERROR CRÍTICO EN LA TAREA DE FONDO:", flush=True)
        traceback.print_exc() # Esto imprimirá el error real si algo falla en el código


# --- EVENTOS DE LA APLICACIÓN ---

@app.on_event("startup")
async def startup_event():
    """Se ejecuta cuando la aplicación se inicia."""
    create_database_and_table()
    if not DB_FILE.stat().st_size > 0: # Si la BD está vacía
        await sync_database()

@app.get("/actualizar-db")
async def update_db_endpoint(background_tasks: BackgroundTasks):
    """Inicia la actualización de la base de datos en segundo plano."""
    background_tasks.add_task(sync_database)
    return RedirectResponse(url="/", status_code=303)


    
# --- Comenzamos con ENDPOINTS - Aquí tendremos tanto los de la web como de la aplicación de React Navite---

@app.get("/", response_class=HTMLResponse) # este sería el index
async def get_all_categories(request: Request): 
    """
    Este endpoint obtiene TODAS las categorías de la API de Mercadona
    y las pasa a la plantilla index.html para que las muestre.
    """
    try:
        cart = request.session.get("cart", {})
        # Sumamos todos los valores (cantidades) del diccionario
        cart_count = sum(cart.values())
        
        async with httpx.AsyncClient() as client:
            response = await client.get(MERCADONA_API_URL)
            response.raise_for_status() 
            
        data = response.json()
        api_data = ApiResponse(**data)
        
        if not api_data.results:
            raise HTTPException(status_code=404, detail="No se encontraron categorías en la API.")
            
    
        return templates.TemplateResponse("index.html", {
            "request": request,
            "main_categories": api_data.results,
            "cart_count": cart_count
        })

    except httpx.RequestError as exc:
        raise HTTPException(status_code=500, detail=f"Error al contactar la API de Mercadona: {exc}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ocurrió un error inesperado: {e}")

@app.get("/api/v1/categories") # este sería el index pero de la API para REACT NATIVE
async def get_json_categories():
    """
    Este endpoint está dedicado a la app móvil.
    Devuelve la lista de categorías en formato JSON puro.
    """
    try:
        url = "https://tienda.mercadona.es/api/categories/"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'}
        
        async with httpx.AsyncClient(headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status() # Lanza un error si la respuesta no es 200 OK, asi podemos manejarlo con HTTPException
            
        # Pasamos el diccionario en una respuesta JSON para que la app lo reconozca correctamente
        return response.json()

    except httpx.RequestError as exc:
        raise HTTPException(status_code=500, detail=f"Error al contactar la API externa: {exc}")
    

@app.get("/api/v1/products/{product_id}")
async def get_product_details(product_id: str):
    """
    Devuelve los detalles completos de un producto específico.
    """
    try:
        url = f"https://tienda.mercadona.es/api/products/{product_id}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'}
        
        async with httpx.AsyncClient(headers=headers) as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Producto con ID {product_id} no encontrado.")
            response.raise_for_status()
            
        return response.json()

    except httpx.RequestError as exc:
        raise HTTPException(status_code=500, detail=f"Error al contactar la API externa: {exc}")
    

@app.get("/api/v1/categories/{category_id}")
async def get_category_products(category_id: int):
    """
    Este endpoint devuelve los detalles y productos de una categoría específica.
    """
    try:
        url = f"https://tienda.mercadona.es/api/categories/{category_id}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'}
        
        async with httpx.AsyncClient(headers=headers) as client:
            response = await client.get(url)
            # Si una categoría no existe, Mercadona devuelve un 404
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Categoría con ID {category_id} no encontrada.")
            response.raise_for_status()
            
        return response.json()

    except httpx.RequestError as exc:
        raise HTTPException(status_code=500, detail=f"Error al contactar la API externa: {exc}")

@app.get("/buscar", response_class=HTMLResponse)
async def search_products(request: Request, query: str):
    """
    Busca productos en la BD por palabras individuales en el nombre, o por EAN/ID exacto,
    y devuelve una página HTML con los resultados.
    """
    cart = request.session.get("cart", {})
    cart_count = sum(cart.values())
    
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Aqui hacemos la lógica de búsqueda y añadimos para que pueda buscar si pones varias palabras y tambíen por ean o id
    search_words = query.strip().split()
    results = []

    # Solo ejecuta la consulta si el usuario ha escrito algo
    if search_words:
        # 1. Crea una condición "LIKE ?" por cada palabra de búsqueda
        name_conditions = " AND ".join(["display_name LIKE ?"] * len(search_words))
        
        # 2. Prepara los parámetros para cada palabra, añadiendo los comodines '%'
        #    Ejemplo: si la búsqueda es "Café Forte", esto será ['%café%', '%forte%']
        name_params = [f"%{word}%" for word in search_words]
        
        # 3. Construye la consulta final, combinando la búsqueda por nombre con la de EAN/ID
        sql_query = f"""
        SELECT * FROM products 
        WHERE ({name_conditions}) OR ean = ? OR id = ?
        """
        # 4. Combina todos los parámetros en el orden correcto
        params = name_params + [query, query]
        
        cursor.execute(sql_query, params)
        results = cursor.fetchall()
    
    conn.close()
    

    # Devuelve la plantilla HTML con los resultados
    return templates.TemplateResponse("resultados.html", {
        "request": request,
        "query": query,
        "results": results,
        "cart_count": cart_count # Pasamos el conteo del carrito.
    })

@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request):
    """
    Genera un sitemap dinámico combinando:
    1. Páginas estáticas (inicio, buscar).
    2. Categorías (consultadas a la API de Mercadona).
    3. Productos (consultados a la BD local).
    """
    # Obtenemos la URL base (ej: http://localhost:8000 o tu dominio real)
    base_url = str(request.base_url).rstrip("/")
    urls = []

    # --- 1. Páginas Estáticas ---
    urls.append(f"{base_url}/")
    urls.append(f"{base_url}/buscar")

    # --- 2. Categorías (Desde la API externa) ---
    # Al igual que haces en el index, consultamos la estructura de categorías
    try:
        async with httpx.AsyncClient() as client:
            # Usamos un timeout corto para no bloquear si la API externa falla
            response = await client.get(MERCADONA_API_URL, timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                # Recorremos los resultados para sacar los IDs de categorías principales
                # Si quieres bajar a nivel de subcategorías, tendrías que iterar 'categories' dentro de cada resultado
                for category in data.get("results", []):
                    # Asumiendo que tu ruta es /categories/{id}/
                    urls.append(f"{base_url}/categories/{category['id']}/")
                    
                    # Opcional: Si quieres incluir las subcategorías también
                    for subcat in category.get("categories", []):
                         urls.append(f"{base_url}/categories/{subcat['id']}/")

    except Exception as e:
        print(f"Error obteniendo categorías para sitemap: {e}")

    # --- 3. Productos (Desde tu SQLite Local) ---
    # Es mucho más rápido leer los productos de tu BD local que de la API
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        # Optimizamos para solo leer la columna ID
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM products")
        products = cursor.fetchall()
        conn.close()

        for prod in products:
            # prod[0] es el id
            urls.append(f"{base_url}/products/{prod[0]}")
            
    except Exception as e:
        print(f"Error obteniendo productos localmente: {e}")

    # --- 4. Construcción del XML ---
    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml_content += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    
    for url in urls:
        xml_content += f'  <url>\n    <loc>{url}</loc>\n    <changefreq>daily</changefreq>\n  </url>\n'
        
    xml_content += '</urlset>'

    return Response(content=xml_content, media_type="application/xml")

# --- ENDPOINTS PARA ACTUALIZAR LA BASE DE DATOS MANUALMENTE, esto lo vamos a ocultar cuando este en producción
# para que no se pueda pulsar cuando se accede, en una siguiente versión añadiremos un acceso privado para esto ---
@app.get("/actualizar-db")
async def update_db_endpoint():
    """Endpoint para disparar la actualización manual de la base de datos."""
    await sync_database()
    # Cuando le damos a actualizar, redirigimos al index 
    return RedirectResponse(url="/", status_code=303)


@app.get("/categories/{category_id}/", response_class=HTMLResponse)
async def read_category(request: Request, category_id: int):
    """
    Obtiene los detalles de una categoría específica (incluyendo sus productos)
    desde la API de Mercadona y los muestra.
    """
    cart = request.session.get("cart", {})
    cart_count = sum(cart.values())
    # Construimos la URL dinámicamente con el ID de la categoría
    category_url = f"https://tienda.mercadona.es/api/categories/{category_id}"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(category_url)
            # Maneja el caso de que una categoría no exista (ej: 404)
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Categoría con ID {category_id} no encontrada.")
            response.raise_for_status()
            
        data = response.json()
        
        # Usamos el nuevo modelo para validar y estructurar los datos
        category_data = CategoryDetail(**data)
        
        # Pasamos los datos completos de la categoría a la plantilla
        return templates.TemplateResponse("categoria.html", {
            "request": request,
            "category": category_data,
            "cart_count": cart_count # Pasamos el conteo del carrito.
        })

    except httpx.RequestError as exc:
        raise HTTPException(status_code=500, detail=f"Error al contactar la API de Mercadona: {exc}")
    except Exception as e:
        
        raise HTTPException(status_code=500, detail=f"Error al procesar los datos: {e}")



@app.get("/products/{product_id}", response_class=HTMLResponse)
async def read_product(request: Request, product_id: str):
    """
    Obtiene los detalles completos de un producto específico desde la API
    y los renderiza en la plantilla productos.html.
    """
    cart = request.session.get("cart", {})
    cart_count = sum(cart.values())
    
    product_url = f"https://tienda.mercadona.es/api/products/{product_id}"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(product_url)
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Producto con ID {product_id} no encontrado.")
            response.raise_for_status()
            
        data = response.json()
        
      
        product_data = ProductDetail(**data)
        
        return templates.TemplateResponse("productos.html", {
            "request": request,
            "product": product_data,
            "cart_count": cart_count # Pasamos el conteo del carrito.
        })

    except httpx.RequestError as exc:
        raise HTTPException(status_code=500, detail=f"Error al contactar la API de Mercadona: {exc}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al procesar los datos del producto: {e}")
    


@app.get("/api/v1/buscar")
async def search_products_api(query: str):
    """
    Busca productos en la BD por palabras individuales en el nombre, o por EAN/ID exacto.
    Devuelve los resultados en formato JSON para la app móvil.
    """
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Este es el inicio de la lógica de búsqueda multi-palabra pero para REACT NATIVE
    search_words = query.strip().split()
    results = []

    # Solo ejecuta la consulta si el usuario ha escrito algo
    if search_words:
        # 1. Crea una condición "LIKE ?" por cada palabra de búsqueda
        name_conditions = " AND ".join(["display_name LIKE ?"] * len(search_words))
        
        # 2. Prepara los parámetros para cada palabra (ej: ['%Café%', '%Forte%'])
        name_params = [f"%{word}%" for word in search_words]
        
        # 3. Construye la consulta final, combinando la búsqueda por nombre con la de EAN/ID
        sql_query = f"""
        SELECT * FROM products 
        WHERE ({name_conditions}) OR ean = ? OR id = ?
        """
        # 4. Combina todos los parámetros en el orden correcto
        params = name_params + [query, query]
        
        cursor.execute(sql_query, params)
        results = cursor.fetchall()
    
    conn.close()
    

    # Devuelve los resultados directamente, los pasamos a JSON para que REACT lo entienda, resolvemos el error de serialización
    return results


# añadimos la parte del carrito

def parse_price(price_str):
    if not price_str:
        return 0.0
    try:
        # Eliminamos el símbolo de euro, espacios y reemplazamos coma por punto
        clean_str = price_str.replace('€', '').replace(',', '.').strip()
        return float(clean_str)
    except ValueError:
        return 0.0

@app.post("/cart/add")
async def add_to_cart(request: Request, product_id: str = Form(...), quantity: int = Form(...)):
    """Añade un producto al carrito (guardado en la sesión)."""
    # Obtenemos el carrito actual de la sesión o creamos uno vacío
    cart = request.session.get("cart", {})
    
    # Si el producto ya está, sumamos la cantidad, si no, lo creamos
    if product_id in cart:
        cart[product_id] += quantity
    else:
        cart[product_id] = quantity
    
    # Guardamos el carrito actualizado en la sesión
    request.session["cart"] = cart
    
    # Redirigimos al usuario a la vista del carrito
    return RedirectResponse(url="/carrito", status_code=303)

@app.post("/cart/update")
async def update_cart(request: Request, product_id: str = Form(...), quantity: int = Form(...), action: str = Form(...)):
    """Actualiza la cantidad o elimina un producto."""
    cart = request.session.get("cart", {})
    
    if action == "delete":
        if product_id in cart:
            del cart[product_id]
    elif action == "update":
        if product_id in cart:
            if quantity > 0:
                cart[product_id] = quantity
            else:
                del cart[product_id] # Si pone 0, lo borramos
                
    request.session["cart"] = cart
    return RedirectResponse(url="/carrito", status_code=303)

@app.get("/carrito", response_class=HTMLResponse)
async def view_cart(request: Request):
    """Muestra los productos que hay en el carrito."""
    cart = request.session.get("cart", {})
    
    cart_items = []
    total_price = 0.0
    
    # Conectamos a la BD para recuperar los detalles (foto, nombre, precio) de los IDs guardados en sesión
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
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
                "thumbnail": product["thumbnail"],
                "unit_price": product["unit_price"], # String original para mostrar
                "quantity": quantity,
                "subtotal": f"{subtotal:.2f} €" # Formateado
            })
            
    conn.close()
    
    return templates.TemplateResponse("carrito.html", {
        "request": request,
        "cart_items": cart_items,
        "total_price": f"{total_price:.2f}"
    })


