# mercadopago

Servicio reutilizable y multi-tenant para integrar Mercado Pago Checkout Pro.

El repositorio mantiene el ciclo de vida del pago, webhooks, idempotencia,
reconciliación y callbacks hacia sistemas consumidores. No conoce entidades
internas de ninguna aplicación: pedidos y turnos se representan mediante
referencias externas opacas.

La rama de integración es `dev`. Ningún workflow despliega producción desde
esta rama.

## Responsabilidad

El servicio es dueño de `payment_intents`, los eventos recibidos del proveedor
y el outbox de callbacks. El consumidor conserva la propiedad de sus pedidos o
turnos y los identifica con `external_reference`; no se comparten tablas ni
claves foráneas entre aplicaciones.

El flujo es:

1. El consumidor crea un intento con `POST /v1/payment-intents`, `X-API-Key` e
   `Idempotency-Key`.
2. El servicio devuelve la URL de Checkout Pro y un token público para
   consultar el estado.
3. Mercado Pago notifica a `/webhooks/mercado-pago`; se valida la firma y el
   monto antes de acreditar.
4. El worker entrega un callback firmado e idempotente al consumidor. Los
   errores se reintentan con backoff y terminan en dead-letter al agotar el
   límite.

En desarrollo, `PAYMENTS_PROVIDER=demo` ofrece el mismo ciclo sin credenciales
reales. El endpoint demo queda deshabilitado automáticamente en producción.

## Ejecución

Copiar `.env.example` a `.env`, configurar PostgreSQL y ejecutar el runner
idempotente antes de iniciar procesos. El runner toma advisory lock, verifica
checksums y aborta si una migración ya aplicada fue modificada.

```bash
pip install -e ".[dev]"
python -m mercadopago_service.migrate
uvicorn mercadopago_service.main:app --host 0.0.0.0 --port 8080
python -m mercadopago_service.callback_worker
python -m mercadopago_service.reconciliation
```

API y worker usan la misma imagen. En Kubernetes deben ejecutarse como
Deployments separados, con el comando del segundo reemplazado por
`python -m mercadopago_service.callback_worker`.
La reconciliación se ejecuta como CronJob con concurrencia prohibida.

## Contrato y seguridad

- Importes: enteros en la unidad de moneda utilizada por Mercado Pago; para
  ARS, `15000` representa ARS 15.000.
- `Idempotency-Key`: estable por operación lógica, mínimo 16 caracteres.
- `PAYMENTS_CALLBACK_ALLOWED_HOSTS`: allowlist explícita contra SSRF.
- Producción exige HTTPS, secretos no predeterminados y credenciales reales.
- Los tokens de estado y callbacks usan firmas HMAC independientes.
- Estados aprobados/refundados no retroceden por notificaciones tardías.

La documentación OpenAPI está disponible en `/docs`. `/health/live` no accede
a dependencias; `/health/ready` valida PostgreSQL.

## Verificación

```bash
ruff check src tests
pytest
pip-audit
```

CI levanta PostgreSQL descartable, aplica las migraciones y ejecuta todas las
pruebas. Ninguna credencial real debe almacenarse en Git.
