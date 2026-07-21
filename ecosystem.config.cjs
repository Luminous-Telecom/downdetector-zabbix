/**
 * PM2 — produção
 *
 * instances: 1 — cache em memória + cron interno (não usar cluster).
 * Variáveis sensíveis ficam no .env (carregado pelo app via dotenv).
 */
module.exports = {
  apps: [
    {
      name: 'downdetector-br',
      script: 'app.js',
      cwd: __dirname,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      watch: false,
      max_memory_restart: '512M',
      kill_timeout: 10_000,
      env: {
        NODE_ENV: 'production',
      },
    },
  ],
};
