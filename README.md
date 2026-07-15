# Downdetector BR → Zabbix

**Cada host no Zabbix = um serviço** (WhatsApp, Instagram, Nubank…).  
O template é o mesmo; o host define o slug via macro.

```
Host "WhatsApp"     {$DOWNDETECTOR.SLUG}=whatsapp
Host "Instagram"    {$DOWNDETECTOR.SLUG}=instagram
        │
        │  Agent interface → IP do coletor
        ▼
Servidor coletor (FlareSolverr + script + zabbix-agent)
```

## 1. Coletor (um servidor)

```bash
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
docker compose up -d

# Timeout=30 no zabbix_agentd.conf
cp zabbix/downdetector.conf /etc/zabbix/zabbix_agentd.d/downdetector.conf
systemctl restart zabbix-agent

zabbix_agentd -t "downdetector.status[whatsapp]"
```

## 2. Importar o template

**Data collection → Templates → Import** →  
arquivo **`zabbix/template_downdetector_br.xml`** (formato Zabbix 5.0+).

No coletor: `git pull` antes de importar. Confirme a versão em
**Administration → General** (ou no rodapé do UI).

## 3. Criar hosts (um por serviço)

Exemplo WhatsApp:

| Campo | Valor |
|---|---|
| Host name | `WhatsApp` |
| Interfaces | Agent → **IP do coletor** (porta 10050) |
| Templates | `Downdetector BR` |
| Macros | `{$DOWNDETECTOR.SLUG}` = `whatsapp` |

Instagram: mesmo template, macro `instagram`.  
Nubank: macro `nubank`.

O slug é a URL: `https://downdetector.com.br/fora-do-ar/<slug>/`

## Itens do template

| Item | Tipo |
|---|---|
| Downdetector: raw | Agent (JSON, 15m) |
| status code / status / relatos / baseline / nome / logo | Dependent |

Triggers: warning se code=2, high se code=3.

## Troubleshooting

- **Timeout**: `Timeout=30` no agent e no Server
- **Unsupported item key**: conf no `zabbix_agentd.d/` + restart
- **403**: FlareSolverr (`docker ps`, porta 8191)
- **Macro errada**: slug tem que bater com a URL do Downdetector
