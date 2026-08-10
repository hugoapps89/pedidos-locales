PEDIDOS LOCALES V6 - ENTREGA $35

NUEVO:
- Se cobra una tarifa fija de entrega de $35.00 por pedido.
- El subtotal de productos se calcula por separado.
- El total enviado al servidor = subtotal + $35.00.
- El cliente ve Productos + Entrega + Total.
- La confirmación muestra el desglose.
- Administrador y repartidor ven el desglose.
- El servidor es quien calcula y guarda el total final, evitando manipulación del precio desde el navegador.

Ejemplo:
Productos: $81.00
Entrega:   $35.00
Total:    $116.00

Instalación:
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py

Admin:
http://127.0.0.1:5000/admin/login
admin / admin1234

Repartidor:
http://127.0.0.1:5000/repartidor/login
carlos / 1234
ana / 1234
