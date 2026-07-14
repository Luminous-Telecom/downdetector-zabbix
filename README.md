# Downdetector BR → Zabbix

Coleta **logo, nome, status** (`success` / `warning` / `danger`) e a
**quantidade de relatos mais recente** de cada serviço listado em
[downdetector.com.br](https://downdetector.com.br/), formatado em JSON para
consumo pelo Zabbix.

## Como funciona

- A **home** do Downdetector lista todos os serviços com nome, logo e status,
  mas **não** traz o número de relatos.
- A **página individual** de cada serviço (`/fora-do-ar/<slug>/`) embute no
  HTML um JSON com os pontos do gráfico de 24h em buckets de 15 minutos:
  `{"timestampUtc", "reportsValue", "baselineValue"}`. O script usa o
  **último ponto** desse array como "relatos atuais" — é o mesmo valor que
  aparece na tooltip do gráfico do site ao passar o mouse (`Relatos: X` /
  `Linha de base: Y`). Não é um contador ao vivo minuto a minuto (isso é
  exclusivo da API paga/Enterprise do Downdetector), mas é o dado público
  mais granular disponível.
- O site é protegido por **Cloudflare** com desafio JS. Requisições simples
  (`requests`, `cloudscraper`, `curl_cffi`) geralmente retornam **403**. O
  script tenta essas alternativas primeiro e cai para o
  **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** (um proxy
  que resolve o desafio usando um Chrome real) quando elas falham.

## 1. Instalação

### 1.1. Dependências Python

```bash
pip install -r requirements.txt --break-system-packages
```

(ou use um virtualenv, se preferir: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`)

### 1.2. FlareSolverr (necessário — o Downdetector bloqueia acesso direto)

```bash
docker compose up -d
```

Isso sobe o FlareSolverr em `http://localhost:8191/v1`, com
`restart: unless-stopped` (sobrevive a reboot). Teste:

```bash
curl -s http://localhost:8191/v1 -H 'Content-Type: application/json' \
  -d '{"cmd":"request.get","url":"https://downdetector.com.br/","maxTimeout":60000}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['solution']['status'])"
```

Deve responder `200`. Se o FlareSolverr estiver em outra máquina/porta, aponte
o script para ele com a variável de ambiente `FLARESOLVERR_URL` (padrão:
`http://localhost:8191/v1`).

## 2. Uso do script

```bash
# Todos os serviços da home (rápido, ~5-10s; sem "reports")
python3 downdetector_scraper.py --pretty

# Um serviço específico (traz "reports"; ~15-25s por causa do Cloudflare)
python3 downdetector_scraper.py --service whatsapp --pretty

# Mesma coisa, só o objeto plano do serviço (formato usado pelo Zabbix)
python3 downdetector_scraper.py --service whatsapp --flat

# Só o código numérico do status (1=success, 2=warning, 3=danger, 0=unknown)
python3 downdetector_scraper.py --service whatsapp --numeric

# Extrair um campo específico do JSON
python3 downdetector_scraper.py --service whatsapp --key reports

# Lista de serviços em formato de Low-Level Discovery do Zabbix
python3 downdetector_scraper.py --lld

# Todos os serviços da home JÁ com "reports" (lento: 1 requisição extra por
# serviço, ~15-25s cada; ~48 serviços ≈ 15-20 minutos)
python3 downdetector_scraper.py --with-reports --pretty

# Igual ao anterior, mas grava cada serviço no arquivo assim que é coletado
# (útil para não perder o progresso se o processo for interrompido)
python3 downdetector_scraper.py --with-reports --output-jsonl saida.jsonl
```

Saída de `--service whatsapp --flat`:

```json
{
  "slug": "whatsapp",
  "name": "Whatsapp",
  "logo": "https://cdn2.downdetector.com/static/uploads/logo/whatsapp-messenger.png",
  "status": "success",
  "status_code": 1,
  "reports": 4,
  "reports_baseline": 5,
  "reports_at": "2026-07-14T23:19:34+00:00",
  "url": "https://downdetector.com.br/fora-do-ar/whatsapp/"
}
```

## 3. Configurar no Zabbix

O agente precisa rodar **no mesmo servidor** onde estão o Python, o script e
o FlareSolverr, já que o `UserParameter` chama tudo localmente.

### 3.1. Instalar o Zabbix Agent 2

```bash
apt install -y zabbix-agent2
```

Edite `/etc/zabbix/zabbix_agent2.conf`:

```ini
Server=<IP do seu Zabbix Server>
ServerActive=<IP do seu Zabbix Server>
Hostname=<nome do host cadastrado no Zabbix>
Timeout=30
```

### 3.2. Aplicar o UserParameter

```bash
mkdir -p /etc/zabbix/zabbix_agent2.d
cp zabbix/downdetector.conf /etc/zabbix/zabbix_agent2.d/downdetector.conf
systemctl restart zabbix-agent2
systemctl enable zabbix-agent2
```

Conteúdo de `zabbix/downdetector.conf`:

```ini
# Descoberta (LLD): lista todos os serviços da home (rápido, sem relatos).
UserParameter=downdetector.discovery,/usr/bin/python3 /caminho/downdetector_scraper.py --lld

# Item mestre: dados completos de UM serviço (logo, nome, status, relatos).
# $1 = slug do serviço, ex.: whatsapp, nubank, steam.
UserParameter=downdetector.status[*],/usr/bin/python3 /caminho/downdetector_scraper.py --service $1 --flat
```

> Ajuste o caminho absoluto do script conforme onde ele estiver instalado.

Teste antes de configurar no frontend:

```bash
zabbix_agent2 -t "downdetector.discovery"
zabbix_agent2 -t "downdetector.status[whatsapp]"
```

### 3.3. Criar o Host

`Data collection → Hosts → Create host`
- **Host name**: o mesmo definido em `Hostname=` no agente
- **Interfaces**: adicione o IP deste servidor (Agent, porta 10050)
- **Templates**: adicione o template criado no passo seguinte

### 3.4. Criar o Template com descoberta automática (LLD)

`Data collection → Templates → Create template` → nome `Downdetector BR`.

**Discovery rule** (`Discovery rules → Create discovery rule`):

| Campo | Valor |
|---|---|
| Name | Downdetector service discovery |
| Type | Zabbix agent |
| Key | `downdetector.discovery` |
| Update interval | `1h` |

**Item prototypes** dentro dessa discovery rule:

**a) Item mestre** — faz a requisição real, uma vez por serviço:

| Campo | Valor |
|---|---|
| Name | `{#NAME}: Downdetector raw` |
| Type | Zabbix agent |
| Key | `downdetector.status[{#SLUG}]` |
| Type of information | Text |
| Update interval | `5m` |
| Custom timeout (Zabbix ≥6.4) | `30s` |
| History storage period | `7d` |

**b) Itens dependentes** — todos com `Type = Dependent item`,
`Master item = {#NAME}: Downdetector raw`, sem nenhuma requisição nova (só
extraem do JSON já coletado pelo item mestre):

| Name | Key | Type of information | JSONPath |
|---|---|---|---|
| `{#NAME}: status` | `downdetector.status.text[{#SLUG}]` | Character | `$.status` |
| `{#NAME}: status code` | `downdetector.status.code[{#SLUG}]` | Numeric (unsigned) | `$.status_code` |
| `{#NAME}: relatos` | `downdetector.reports[{#SLUG}]` | Numeric (unsigned) | `$.reports` |
| `{#NAME}: baseline de relatos` | `downdetector.reports.baseline[{#SLUG}]` | Numeric (unsigned) | `$.reports_baseline` |
| `{#NAME}: logo` | `downdetector.logo[{#SLUG}]` | Character | `$.logo` |
| `{#NAME}: nome` | `downdetector.name[{#SLUG}]` | Character | `$.name` |

Em cada item dependente, adicione um passo de pré-processamento:
`Type = JSON Path`, `Parameters = $.status` (ajuste por linha da tabela).
Marque **"Custom on fail" → Discard value** para não gerar erro se algum
campo faltar numa coleta pontual com falha.

**Trigger prototypes**:

| Name | Expression | Severity |
|---|---|---|
| `{#NAME}: Downdetector reporta problemas` | `last(/Downdetector BR/downdetector.status.code[{#SLUG}])=3` | High |
| `{#NAME}: Downdetector reporta possíveis problemas` | `last(/Downdetector BR/downdetector.status.code[{#SLUG}])=2` | Warning |

### Por que item mestre + dependentes (e não 1 item por campo)

Se cada campo (status, reports, logo, nome) fosse um item `Zabbix agent`
separado chamando o script direto, o Zabbix dispararia **4 requisições ao
Cloudflare por serviço** a cada ciclo — 4x mais lento e 4x mais chance de
bloqueio. Com item mestre + dependentes, é **1 requisição por serviço**, e
todos os campos são extraídos do mesmo JSON.

## 4. Troubleshooting

- **`HTTP 403` / "página de desafio Cloudflare"**: o FlareSolverr não está
  rodando ou não está acessível. Confira `docker ps` e
  `curl http://localhost:8191/v1`.
- **Zabbix agent retorna vazio/timeout**: aumente `Timeout=30` no
  `zabbix_agent2.conf` (e, em versões <6.4, também no `zabbix_server.conf` /
  `zabbix_proxy.conf`, já que o timeout do agente é limitado pelo do
  servidor nesses casos). Cada chamada ao script pode levar até ~25-30s.
- **Muitos serviços descobertos e FlareSolverr sobrecarregado**: aumente o
  `Update interval` dos itens mestre (ex.: `10m` ou `15m`) — o status de um
  serviço não muda a cada poucos minutos na maioria dos casos.
- **Quer testar sem depender do FlareSolverr**: use
  `--fetcher requests` ou `--fetcher curl_cffi` — funcionam se o Cloudflare
  não estiver desafiando aquele IP/sessão específico, mas não são
  garantidos.

## Estrutura do projeto

```
downdetector_scraper.py     # script principal
docker-compose.yml          # FlareSolverr
requirements.txt            # dependências Python
zabbix/downdetector.conf    # UserParameters prontos para copiar
```
