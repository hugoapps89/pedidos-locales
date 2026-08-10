# Pedidos Locales V7

Aplicación Flask preparada para subir a GitHub y desplegar como Web Service en Render.

## Desarrollo local

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Abrir http://127.0.0.1:5000

## GitHub

```powershell
git init
git add .
git commit -m "Pedidos Locales V7"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/pedidos-locales.git
git push -u origin main
```

## Render

Build Command:
`pip install -r requirements.txt`

Start Command:
`gunicorn app:app`

Variables:
- `SECRET_KEY`: usa el valor generado por Render si despliegas con render.yaml.
- `ADMIN_USER`: usuario administrativo.
- `ADMIN_PASSWORD`: contraseña administrativa.
- `FLASK_ENV=production`
- `FLASK_DEBUG=0`
- `DATABASE_PATH=/opt/render/project/src/storage/pedidos_locales.db`

IMPORTANTE:
SQLite necesita almacenamiento persistente para conservar pedidos y cambios después de reinicios/redeploys. Render indica que el filesystem normal de un servicio es efímero; un Persistent Disk conserva cambios, pero es una función de servicios de pago. Para una instalación comercial recomendamos migrar la aplicación a Render Postgres antes de ponerla en producción.

## Seguridad

- Las contraseñas de repartidores nuevas se almacenan con hash.
- No subas `.env`, `pedidos_locales.db` ni `venv`.
- Cambia las credenciales de administrador antes de publicar.
