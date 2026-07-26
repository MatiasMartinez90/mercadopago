# mercadopago

Servicio reutilizable y multi-tenant para integrar Mercado Pago Checkout Pro.

El repositorio mantiene el ciclo de vida del pago, webhooks, idempotencia,
reconciliación y callbacks hacia sistemas consumidores. No conoce entidades
internas de ninguna aplicación: pedidos y turnos se representan mediante
referencias externas opacas.

La rama de integración es `dev`. Ningún workflow despliega producción desde
esta rama.
