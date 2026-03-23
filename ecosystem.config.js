module.exports = {
	apps: [
		{
			name: 'PRONTO_API_IMAGES_MANAGER',
			script: './venv/bin/uvicorn',
			args: 'app.main:app --host 0.0.0.0 --port 3600',
			interpreter: 'none',
			cwd: __dirname,
			autorestart: true,
			max_memory_restart: '2000M',
			env_file: '.env',
		},
	],
};
