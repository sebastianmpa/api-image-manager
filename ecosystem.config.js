module.exports = {
  apps: [
    {
      name: "PRONTO-API-IMAGE-MANAGER",
      script: "uvicorn",
      args: "app.main:app --reload --host 0.0.0.0 --port 3600",
      interpreter: "C:\\Users\\sebastian\\Desktop\\optimized_image\\.venv\\Scripts\\python",
      watch: true,
      ignore_watch: ["node_modules", "__pycache__", ".venv", "*.log"],
      env: {
        NODE_ENV: "development",
      },
      error_file: "./logs/error.log",
      out_file: "./logs/out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
  ],
};
