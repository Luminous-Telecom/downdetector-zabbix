# Downdetector BR → Zabbix 7.0

**Sem Zabbix Agent.** Cadastro só na web.  
Cada host dispara um **External check** a cada 5 min; o Server roda o script.

```
Zabbix web: host "caixa" + template downdetector
        │
        │  External check a cada 5m: downdetector[caixa]
        ▼
Zabbix Server ──► /usr/lib/zabbix/externalscripts/downdetector
                └─► lê /var/cache/.../caixa.json  (ms)

Timer ──API──► hosts do template ──► FlareSolverr (**N em paralelo**) ──► cache/
```

## 1. Coletor / Server (mesmo servidor)

```bash
cd /opt/downdetector-zabbix
git pull
pip install -r requirements.txt --break-system-packages
docker compose up -d

mkdir -p /var/cache/downdetector-zabbix
chown -R zabbix:zabbix /var/cache/downdetector-zabbix

# Script do External check (path padrão Debian/Ubuntu)
cp zabbix/externalscripts/downdetector /usr/lib/zabbix/externalscripts/downdetector
chmod 755 /usr/lib/zabbix/externalscripts/downdetector
# confira: grep ^ExternalScripts /etc/zabbix/zabbix_server.conf
```

Teste manual:

```bash
/usr/lib/zabbix/externalscripts/downdetector whatsapp
```

## 2. API token (timer descobre os hosts)

**Users → API tokens → Create**

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

Importe de novo: `zabbix/template_downdetector_br.yaml`  
(item mestre = **External check** `downdetector[{HOST.HOST}]`).

| Campo | Valor |
|---|---|
| Host name | slug (`caixa`, `whatsapp`, `banco-inter`) |
| Visible name | Caixa Econômica Federal, etc. |
| Interfaces | pode ser Agent `127.0.0.1` (não precisa agent rodando) |
| Templates | `downdetector` |

Não precisa UserParameter / agent. O ZBX pode ficar cinza — o que importa é o item External.

## Troubleshooting

- **Unsupported item key / empty**: script não está em `ExternalScripts` ou sem `chmod +x`
- **cache ausente**: rode `systemctl start downdetector-refresh.service`
- **Session terminated** na API: token/URL em `/etc/zabbix/downdetector-api.env`
- **Timeout no External**: Timeout do Server ≥ 10s (leitura de cache é rápida)
