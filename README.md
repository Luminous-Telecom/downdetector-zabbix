# Downdetector BR → Zabbix

Monitora **só os serviços que você cadastrar** no Zabbix (sem discovery,
sem cache, sem timer). Cada item chama o script para um slug.

Exemplo de slug: `whatsapp`, `nubank`, `cloudflare`
→ URL: https://downdetector.com.br/fora-do-ar/whatsapp/

## O que você precisa

1. Arquivos em `/opt/downdetector-zabbix`
2. Docker + FlareSolverr (Cloudflare)
3. Zabbix Agent com `Timeout=30`
4. Um item por serviço no host

## 1. Dependências

```bash
apt update
apt install -y python3 python3-pip curl ca-certificates
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
```

## 2. FlareSolverr

```bash
cd /opt/downdetector-zabbix
docker compose up -d
# ou: apt install docker.io && docker compose up -d
```

## 3. Testar

```bash
python3 /opt/downdetector-zabbix/downdetector_scraper.py --service whatsapp --pretty
```

Deve retornar JSON com `status`, `status_code`, `reports`, `logo`, `name`
(leva ~5–20s por causa do Cloudflare).

## 4. Agent

```bash
# /etc/zabbix/zabbix_agentd.conf (ou agent2.conf)
Timeout=30

cp /opt/downdetector-zabbix/zabbix/downdetector.conf \
   /etc/zabbix/zabbix_agentd.d/downdetector.conf
systemctl restart zabbix-agent

zabbix_agentd -t "downdetector.status[whatsapp]"
```

## 5. Itens no Zabbix (manual)

Crie **só** os serviços que importam. Exemplo WhatsApp:

**Item mestre**

| Campo | Valor |
|---|---|
| Name | WhatsApp Downdetector |
| Type | Zabbix agent |
| Key | `downdetector.status[whatsapp]` |
| Type of information | Text |
| Update interval | `15m` |
| Timeout | `30s` |

**Itens dependentes** (Master = item acima):

| Name | Key | Tipo | JSONPath |
|---|---|---|---|
| WhatsApp status code | `dd.whatsapp.code` | Numeric unsigned | `$.status_code` |
| WhatsApp relatos | `dd.whatsapp.reports` | Numeric unsigned | `$.reports` |
| WhatsApp status | `dd.whatsapp.status` | Character | `$.status` |

Códigos: `1` = ok · `2` = warning · `3` = danger · `0` = unknown

**Trigger exemplo**

```
last(/HOST/dd.whatsapp.code)=3
```

Repita a mesma ideia trocando `whatsapp` pelo slug que quiser
(`nubank`, `cloudflare`, …).

> Não use discovery da home. Não crie dezenas de itens — cada um passa
> pelo FlareSolverr (~5–20s). Poucos serviços + intervalo 15m é o ideal.

## Troubleshooting

- **HTTP 403 / Cloudflare**: `docker ps` e `curl http://localhost:8191/`
- **Timeout no agent**: `Timeout=30` no agent e no Server (Administration → General → Timeouts)
- **Unsupported item key**: conf no `zabbix_agentd.d/` e restart do agent
- **Muitos network errors**: menos itens ou intervalo maior (não rode 50 serviços)
