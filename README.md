# Downdetector BR → Zabbix 7.0

**Cada host = um serviço** (WhatsApp, Instagram…).  
Template `downdetector` + macro `{$DOWNDETECTOR.SLUG}` no host.

```
Host "WhatsApp"     {$DOWNDETECTOR.SLUG}=whatsapp
Host "Instagram"    {$DOWNDETECTOR.SLUG}=instagram
        │  Agent → IP do coletor
        ▼
Coletador (FlareSolverr + script + zabbix-agent)
```

## 1. Coletor

```bash
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
docker compose up -d

# Timeout=30 no agent
cp zabbix/downdetector.conf /etc/zabbix/zabbix_agentd.d/downdetector.conf
systemctl restart zabbix-agent

zabbix_agentd -t "downdetector.status[whatsapp]"
```

## 2. Importar o template (Zabbix 7.0)

**Data collection → Templates → Import** →  
`zabbix/template_downdetector_br.yaml`

## 3. Criar hosts

| Campo | Valor |
|---|---|
| Host name | `WhatsApp` |
| Interfaces | Agent → IP do coletor :10050 |
| Templates | `downdetector` |
| Macros | `{$DOWNDETECTOR.SLUG}` = `whatsapp` |

Slug = URL: `https://downdetector.com.br/fora-do-ar/<slug>/`

## Troubleshooting

- **Timeout**: `Timeout=30` no agent e no Server
- **Unsupported item key**: conf no agentd.d + restart
- **403**: FlareSolverr na porta 8191
