# Downdetector BR → Zabbix 7.0

**Cada host = um serviço** (`Host name` = slug).  
O **agent só lê cache** (ms). Um **timer systemd** atualiza o FlareSolverr em série.

```
Timer (a cada 5 min) ──► FlareSolverr ──► /var/cache/.../slug.json
Zabbix (N hosts)     ──► agent --from-cache ──► lê o JSON (sem Cloudflare)
```

## 1. Coletor

```bash
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
docker compose up -d

cp services.txt.example services.txt
nano services.txt   # TODOS os Host name que você criou no Zabbix

mkdir -p /var/cache/downdetector-zabbix
chown -R zabbix:zabbix /var/cache/downdetector-zabbix /opt/downdetector-zabbix/services.txt

cp zabbix/downdetector.conf /etc/zabbix/zabbix_agentd.d/downdetector.conf
# Timeout=30 no agentd (máximo do clássico)

cp systemd/downdetector-refresh.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now downdetector-refresh.timer
systemctl start downdetector-refresh.service   # 1ª carga (pode demorar)

systemctl restart zabbix-agent
zabbix_agentd -t "downdetector.status[whatsapp]"   # deve ser instantâneo
```

Acompanhe o refresh: `journalctl -u downdetector-refresh.service -f`

## 2. Template + hosts

Importe `zabbix/template_downdetector_br.yaml`.

| Campo | Valor |
|---|---|
| Host name | slug (`caixa`, `banco-inter`, `whatsapp`) |
| Visible name | nome amigável |
| Agent | IP do coletor :10050 |
| Templates | `downdetector` |

**Todo host novo:** acrescente o slug em `services.txt` e rode  
`systemctl start downdetector-refresh.service` (ou espere o timer).

## Troubleshooting

- **Agent cai / ZBX vermelho com muitos hosts**: confira se o UserParameter
  tem `--from-cache` (não `--cache-ttl`). O timer faz o scrape.
- **cache ausente**: slug faltando em `services.txt` ou refresh ainda não rodou
- **Permission denied**: `chown -R zabbix:zabbix /var/cache/downdetector-zabbix`
- **Timeout=60 rejeitado**: agentd clássico máximo é **30**
- **403**: FlareSolverr (`docker ps`, porta 8191)
