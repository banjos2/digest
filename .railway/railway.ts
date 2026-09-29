// Railway project for the digest service. Runbook: docs/RAILWAY_DEPLOYMENT.md.
// Secrets are Railway shared variables (ctx.shared.*); they are never written here.
import { database, defineRailway, github, image, project, service, volume } from "railway/iac";

export default defineRailway((ctx) => {
  // Postgres 17 matches compose, CI and the pg_dump in the app image (Debian postgresql-client 17).
  const db = database("Postgres", "postgres", {
    image: "ghcr.io/railwayapp-templates/postgres-ssl:17",
    output: "DATABASE_URL",
    defaultMountPath: "/var/lib/postgresql/data",
  });

  const brokerData = volume("rabbitmq-data");
  const broker = service("rabbitmq", {
    source: image("rabbitmq:4-management"),
    volumeMounts: { "/var/lib/rabbitmq": brokerData },
    env: {
      // A fixed node name keeps the broker state on the volume across redeploys.
      RABBITMQ_NODENAME: "rabbit@localhost",
      RABBITMQ_DEFAULT_USER: "digest",
      RABBITMQ_DEFAULT_PASS: ctx.shared.RABBITMQ_PASSWORD,
    },
  });

  const app = {
    DJANGO_DEBUG: "0",
    DJANGO_SECRET_KEY: ctx.shared.DJANGO_SECRET_KEY,
    ERASURE_HASH_KEY: ctx.shared.ERASURE_HASH_KEY,
    POSTGRES_HOST: db.env.PGHOST,
    POSTGRES_PORT: db.env.PGPORT,
    POSTGRES_DB: db.env.PGDATABASE,
    POSTGRES_USER: db.env.PGUSER,
    POSTGRES_PASSWORD: db.env.PGPASSWORD,
    CELERY_BROKER_URL:
      "amqp://digest:${{shared.RABBITMQ_PASSWORD}}@${{rabbitmq.RAILWAY_PRIVATE_DOMAIN}}:5672//",
    SOURCE_HTTP_USER_AGENT: ctx.shared.SOURCE_HTTP_USER_AGENT,
    OPENROUTER_API_KEY: ctx.shared.OPENROUTER_API_KEY,
    AI_MONTHLY_BUDGET_USD: "25.00",
    TELEGRAM_BOT_TOKEN: ctx.shared.TELEGRAM_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET: ctx.shared.TELEGRAM_WEBHOOK_SECRET,
    TELEGRAM_SOURCE_API_ID: ctx.shared.TELEGRAM_SOURCE_API_ID,
    TELEGRAM_SOURCE_API_HASH: ctx.shared.TELEGRAM_SOURCE_API_HASH,
    TELEGRAM_SOURCE_PHONE: ctx.shared.TELEGRAM_SOURCE_PHONE,
    BACKUP_AGE_RECIPIENT: ctx.shared.BACKUP_AGE_RECIPIENT,
  };

  const repo = github("banjos2/digest", { branch: "main" });

  // "digest" is the public web service; its generated domain targets port 8000.
  const web = service("digest", {
    source: repo,
    build: { builder: "DOCKERFILE" },
    // gunicorn binds 0.0.0.0:$PORT.
    start: "gunicorn digest_service.wsgi:application --workers 2 --timeout 90 --access-logfile - --error-logfile -",
    preDeploy: "sh deploy/init.sh",
    healthcheck: "/health/",
    env: {
      ...app,
      PORT: "8000",
      DJANGO_ALLOWED_HOSTS: "${{RAILWAY_PUBLIC_DOMAIN}},healthcheck.railway.app",
      DJANGO_CSRF_TRUSTED_ORIGINS: "https://${{RAILWAY_PUBLIC_DOMAIN}}",
      DJANGO_TRUST_PROXY: "1",
    },
  });

  const workerData = volume("worker-data");
  const worker = service("worker", {
    source: repo,
    build: { builder: "DOCKERFILE" },
    start: "bash deploy/railway-worker.sh",
    volumeMounts: { "/data": workerData },
    // Time for SIGTERM to release process leases; longer jobs are retried after their lease expires.
    deploy: { drainingSeconds: 120 },
    env: {
      ...app,
      TELEGRAM_SOURCE_SESSION_PATH: "/data/telethon/source",
      BACKUP_OUTPUT_DIR: "/data/backups",
      ERASURE_GUARD_OUTPUT_DIR: "/data/erasure-guard",
      BACKUP_SCHEDULE_ENABLED: "1",
      // Railway mounts volumes as root; the image user (uid 10001) cannot write there.
      RAILWAY_RUN_UID: "0",
    },
  });

  return project("ai-digest", {
    resources: [db, broker, brokerData, web, worker, workerData],
  });
});
