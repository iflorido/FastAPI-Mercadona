# 🛒 FastAPI + React Native: Productos Mercadona

### Desarrollado por Ignacio Florido - 👨🏻‍💻 https://cv.iflorido.es 

Aplicación desarrollada en **FastAPI** que consume la API de **Mercadona** para obtener y mostrar sus productos.  
Incluye una interfaz web con **Tailwind CSS** y **Jinja2**, y sirve también como **backend** para una app móvil en **React Native**. Crea una base de datos inicial, que se puede actualizar en segundo plano para que las búsquedas sean más rápidas, una vez encontrado el producto hace la consulta a la API.

## ✨ Características

- 📦 Listado de productos actualizados desde la API de Mercadona.  
- 🔍 Búsqueda de productos por nombre.
- 🔍 Búsqueda rápida ya que almacena inicialmente los productos en una SQL
- 📱 Soporte para **app móvil** con React Native.  
- 📷 Escaneo de códigos **EAN** usando la cámara del móvil.  
- 🎨 Interfaz web moderna y responsive con **Tailwind CSS**.


💻 - Dependencias pip install -r requirements.txt 
💻 - Ejecución **uvicorn main:app --reload**
