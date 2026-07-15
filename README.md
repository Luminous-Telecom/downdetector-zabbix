# Downdetector BR → Zabbix 7.0

**Cada host = um serviço.** O slug é o **Host name** técnico (`{HOST.HOST}`).
Sem macro.

```
Host name: whatsapp      Visible name: WhatsApp
Host name: instagram     Visible name: Instagram
        │  Agent → IP do coletor
        ▼
Coletador (FlareSolverr + script + zabbix-agent)
```

## 1. Coletor

```bash
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
docker compose up -d

# Timeout=30 no agent (máximo do agentd clássico; 60 é rejeitado)
cp zabbix/downdetector.conf /etc/zabbix/zabbix_agentd.d/downdetector.conf
mkdir -p /var/cache/downdetector-zabbix
chown -R zabbix:zabbix /var/cache/downdetector-zabbix
chmod 775 /var/cache/downdetector-zabbix
systemctl restart zabbix-agent

# 1ª chamada pode demorar (~FlareSolverr); seguintes <1s (cache 10 min)
zabbix_agentd -t "downdetector.status[whatsapp]"
```

## 2. Template

**Data collection → Templates → Import** →  
`zabbix/template_downdetector_br.yaml`

## 3. Criar hosts

| Campo | Valor |
|---|---|
| Host name | `whatsapp` (slug da URL, minúsculo) |
| Visible name | `WhatsApp` (opcional, só exibição) |
| Interfaces | Agent → IP do coletor :10050 |
| Templates | `downdetector` |

URL: `https://downdetector.com.br/fora-do-ar/<Host name>/`

## Troubleshooting

- **Permission denied** em `/var/cache/downdetector-zabbix`:
  `chown -R zabbix:zabbix /var/cache/downdetector-zabbix`
- **Timeout**: agentd clássico aceita no máximo **30** (60 é rejeitado e o agent não sobe)
- **read timeout / ZBX vermelho**: use cache (`--cache-ttl 600` no conf) e item raw = 10m
- **Unsupported item key**: conf no agentd.d + restart
- **403**: FlareSolverr na porta 8191
- **Item não acha serviço**: Host name tem que ser o slug (`whatsapp`, não `WhatsApp`)
