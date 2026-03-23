module.exports = {
  apps: [
    {
      name: "PRONTO-API-IMAGE-MANAGER",
      script: "./venv/bin/uvicorn",
      args: "app.main:app --host 0.0.0.0 --port 3600",
      cwd: "/home/administrator/apps/services/api-image-manager",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "300M"
    }
  ]
};
