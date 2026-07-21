# Downdetector BR → Zabbix Agent (rápido)

**Agent responde em ms** (só lê cache).  
**Hosts manuais** no Zabbix (um por serviço).  
**Timer** atualiza o cache via FlareSolverr a cada 5 min.

```
Host name: whatsapp   Visible: WhatsApp
Host name: caixa      Visible: Caixa Econômica
        │  Agent → IP do coletor :10050
        ▼
Agent: downdetector.status[{HOST.HOST}]  →  lê /var/cache/.../slug.json  (ms)

Timer (5 min) → API Zabbix (lista hosts) → FlareSolverr (2x paralelo) → cache
```

## 1. Coletor

```bash
cd /opt/downdetector-zabbix   # ou clone/copie o projeto aqui
pip install -r requirements.txt --break-system-packages
docker compose up -d

mkdir -p /var/cache/downdetector-zabbix
chown -R zabbix:zabbix /var/cache/downdetector-zabbix

# Timeout=30 no /etc/zabbix/zabbix_agentd.conf
cp zabbix/downdetector.conf /etc/zabbix/zabbix_agentd.d/downdetector.conf
systemctl restart zabbix-agent
```

## 2. API token (timer descobre os hosts sozinho)

No Zabbix: **Users → API tokens → Create**

```bash
cp zabbix/downdetector-api.env.example /etc/zabbix/downdetector-api.env
# edite ZABBIX_URL + ZABBIX_TOKEN (sem aspas)
chmod 640 /etc/zabbix/downdetector-api.env
chown root:zabbix /etc/zabbix/downdetector-api.env

cp systemd/downdetector-refresh.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now downdetector-refresh.timer
systemctl start downdetector-refresh.service
journalctl -u downdetector-refresh.service -f
```

## 3. Template + hosts (só na web)

Importe `zabbix/template_downdetector_br.yaml`.

Para cada serviço:

| Campo | Exemplo |
|---|---|
| Host name | `whatsapp` / `caixa` / `banco-inter` |
| Visible name | WhatsApp / Caixa Econômica Federal |
| Agent | IP do coletor, porta **10050** |
| Templates | `downdetector` |

Slug = URL: `https://downdetector.com.br/fora-do-ar/<Host name>/`

Teste no coletor:

```bash
zabbix_agentd -t "downdetector.status[whatsapp]"
# deve retornar JSON em <1s (depois do 1º refresh)
```

## Por que é rápido

| Parte | Tempo |
|---|---|
| Agent (Zabbix) | ~ms — só lê arquivo |
| Timer (background) | ~1–3 min para ~40 hosts (2 Chromes) |

Não rode FlareSolverr dentro do agent: Timeout máximo do agentd é **30s** e o Cloudflare costuma passar disso.

## Troubleshooting

- **cache ausente**: `systemctl start downdetector-refresh.service`
- **Permission denied** no cache: `chown -R zabbix:zabbix /var/cache/downdetector-zabbix`
- **Session terminated** na API: token/URL em `/etc/zabbix/downdetector-api.env`
- **CPU alta no refresh**: baixe `--workers 1` no unit systemd
- **Timeout=60 rejeitado**: agentd clássico aceita no máximo **30**
