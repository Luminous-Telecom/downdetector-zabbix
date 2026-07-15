# Downdetector BR → Zabbix

**Template geral** no Zabbix. O **host** (agent) decide quais serviços
coletar via arquivo local `services.txt` e executa o scrape.

```
Template "Downdetector BR"
        │  (vinculado ao host)
        ▼
Host zabbix-grafana (ou outro)
  ├─ services.txt          → discovery (LLD)
  └─ FlareSolverr + script → item downdetector.status[slug]
```

## Instalação no host coletor

```bash
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
docker compose up -d

cp services.txt.example services.txt
nano services.txt          # só os slugs que este host deve monitorar

# Agent: Timeout=30
cp zabbix/downdetector.conf /etc/zabbix/zabbix_agentd.d/downdetector.conf
systemctl restart zabbix-agent

zabbix_agentd -t downdetector.discovery
zabbix_agentd -t "downdetector.status[whatsapp]"
```

Exemplo de `services.txt`:

```
whatsapp|WhatsApp
nubank|Nubank
cloudflare|Cloudflare
```

## Template no Zabbix Server

1. **Data collection → Templates → Import**
2. Arquivo: `zabbix/template_downdetector_br.yaml`
3. No host coletor: **Templates → Link** → `Downdetector BR`
4. Interface Agent do host apontando para o IP onde o script/FlareSolverr rodam
5. Aguarde a discovery (até 1h, ou **Execute now** na discovery rule)

O template cria automaticamente (por slug da lista):

| Item | Tipo | Notas |
|---|---|---|
| `downdetector.status[{#SLUG}]` | Agent (Text) | JSON; intervalo 15m |
| `…status.code` | Dependent | 1/2/3/0 |
| `…status.text` | Dependent | success/warning/danger |
| `…reports` / `…baseline` | Dependent | relatos |
| `…name` / `…logo` | Dependent | texto |

Triggers: warning se code=2, high se code=3.

## Hosts diferentes, listas diferentes

Cada host com o template pode ter seu próprio `services.txt`.
Ex.: host A monitora ISPs; host B monitora bancos.

> Poucos serviços por host. Cada `status[*]` leva ~5–20s (Cloudflare).

## Troubleshooting

- **Discovery vazia**: falta `services.txt` ou path errado
- **Timeout / network error**: `Timeout=30` no agent e no Server; menos itens
- **403**: FlareSolverr (`docker ps`, porta 8191)
