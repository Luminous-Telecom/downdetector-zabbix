# Downdetector BR → Zabbix 7.0

**Cadastro só no Zabbix web.** Cada host = um serviço (`Host name` = slug).

```
Zabbix web (hosts + template)
        │
        ▼
Timer ──API──► lista Host names ──► FlareSolverr (1 a 1) ──► cache/
Agent ───────────────────────────────────────────────────► lê cache (ms)
```

## 1. Coletor

```bash
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
docker compose up -d

mkdir -p /var/cache/downdetector-zabbix
chown -R zabbix:zabbix /var/cache/downdetector-zabbix

# Timeout=30 no agentd (máximo do clássico)
cp zabbix/downdetector.conf /etc/zabbix/zabbix_agentd.d/downdetector.conf
systemctl restart zabbix-agent
```

## 2. API token (para o timer achar os hosts)

No Zabbix: **Users → API tokens → Create** (usuário com permissão de ler hosts).

```bash
cp /opt/downdetector-zabbix/zabbix/downdetector-api.env.example \
   /etc/zabbix/downdetector-api.env
nano /etc/zabbix/downdetector-api.env   # só URL + TOKEN (uma vez)
chmod 640 /etc/zabbix/downdetector-api.env
chown root:zabbix /etc/zabbix/downdetector-api.env
```

URL típica: `http://127.0.0.1/api_jsonrpc.php`  
(ou `http://127.0.0.1/zabbix/api_jsonrpc.php`)

## 3. Timer de atualização

```bash
cp systemd/downdetector-refresh.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now downdetector-refresh.timer
systemctl start downdetector-refresh.service
journalctl -u downdetector-refresh.service -f
```

## 4. Template + hosts (só na web)

Importe `zabbix/template_downdetector_br.yaml`.

Para cada serviço:

| Campo | Valor |
|---|---|
| Host name | slug (`caixa`, `banco-inter`, `whatsapp`) |
| Visible name | Caixa Econômica Federal, etc. |
| Agent | `127.0.0.1:10050` (coletor) |
| Templates | `downdetector` |

Pronto — **não precisa editar lista de serviços no servidor**.  
Host novo na web → no próximo ciclo do timer (5 min) o cache é criado.  
Forçar agora: `systemctl start downdetector-refresh.service`

## Troubleshooting

- **cache ausente**: host novo ainda não passou pelo timer; rode o service acima
- **API token / template não encontrado**: confira `/etc/zabbix/downdetector-api.env`
- **Permission denied** no cache: `chown -R zabbix:zabbix /var/cache/downdetector-zabbix`
- **Timeout=60 rejeitado**: agentd máximo é **30**
- **403**: FlareSolverr (`docker ps`)
