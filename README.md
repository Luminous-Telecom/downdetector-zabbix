# Downdetector BR → Zabbix

Coleta **logo, nome, status** (`success` / `warning` / `danger`) e a
**quantidade de relatos mais recente** de cada serviço listado em
[downdetector.com.br](https://downdetector.com.br/), formatado em JSON para
consumo pelo Zabbix.

Este guia parte de um servidor **Debian 13 (trixie) limpo**, sem nada
instalado, até o Zabbix monitorando os serviços. Se algum pacote já estiver
instalado no seu servidor, o `apt install` correspondente simplesmente não
faz nada (é seguro rodar de novo).

## Índice

1. [Como funciona](#como-funciona)
2. [Pré-requisitos do sistema](#1-pré-requisitos-do-sistema)
3. [Instalar o Docker](#2-instalar-o-docker)
4. [Instalar as dependências Python](#3-instalar-as-dependências-python)
5. [Subir o FlareSolverr](#4-subir-o-flaresolverr)
6. [Testar o script](#5-testar-o-script)
7. [Instalar e configurar o Zabbix Agent](#6-instalar-e-configurar-o-zabbix-agent)
8. [Criar o Template no Zabbix](#7-criar-o-template-no-zabbix)
9. [Troubleshooting](#8-troubleshooting)

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
  que resolve o desafio usando um Chrome real dentro de um container Docker)
  quando elas falham.

## 1. Pré-requisitos do sistema

```bash
apt update
apt install -y \
  git \
  wget \
  curl \
  ca-certificates \
  gnupg \
  python3 \
  python3-pip \
  python3-venv
```

O que cada um faz aqui:
- **git** — controle de versão, útil para manter o projeto atualizado
- **wget** / **curl** — baixar coisas e testar endpoints (FlareSolverr, etc.)
- **ca-certificates**, **gnupg** — necessários para adicionar o repositório
  oficial do Docker com segurança (passo 2)
- **python3**, **python3-pip** — rodar o script e instalar as bibliotecas
- **python3-venv** — opcional, caso prefira isolar as dependências num
  virtualenv em vez de instalar no sistema

## 2. Instalar o Docker

O FlareSolverr roda em um container Docker. Instalação oficial (Docker CE),
recomendada pela própria Docker para Debian:

```bash
# Adiciona a chave GPG oficial do Docker
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# Adiciona o repositório do Docker
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Confirme que instalou certo:

```bash
docker --version
docker compose version
```

Ative o serviço (normalmente já vem ativo, mas garanta):

```bash
systemctl enable --now docker
```

> Alternativa mais simples (versão mais antiga, direto do repositório do
> Debian, sem o `docker compose` v2): `apt install -y docker.io`. Se usar
> essa opção, troque `docker compose` por `docker-compose` (com hífen) nos
> comandos deste guia — mas recomendamos o método oficial acima.

## 3. Instalar as dependências Python

Este guia pressupõe que os arquivos do projeto já estão em
**`/opt/downdetector-zabbix`** neste servidor — esse é o caminho fixo que o
`zabbix/downdetector.conf` espera, então o `UserParameter` funciona sem
precisar editar nada. Se os arquivos estiverem em outro lugar, ajuste os
caminhos em `zabbix/downdetector.conf` antes do passo 6.3.

```bash
cd /opt/downdetector-zabbix
pip install -r requirements.txt --break-system-packages
```

`--break-system-packages` é necessário em Debian 13 porque o Python do
sistema bloqueia `pip install` fora de um virtualenv por padrão. Se preferir
isolar num virtualenv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# use .venv/bin/python3 no lugar de python3 nos comandos abaixo
```

## 4. Subir o FlareSolverr

```bash
docker compose up -d
```

Isso sobe o FlareSolverr em `http://localhost:8191/v1`, com
`restart: unless-stopped` (sobrevive a reboot). Aguarde ~10s para o
container inicializar e teste:

```bash
curl -s http://localhost:8191/v1 -H 'Content-Type: application/json' \
  -d '{"cmd":"request.get","url":"https://downdetector.com.br/","maxTimeout":60000}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['solution']['status'])"
```

Deve responder `200`. Se der erro de conexão, confira `docker ps` e
`docker logs flaresolverr`.

Se o FlareSolverr estiver em outra máquina/porta, aponte o script para ele
com a variável de ambiente `FLARESOLVERR_URL` (padrão:
`http://localhost:8191/v1`):

```bash
export FLARESOLVERR_URL="http://outro-host:8191/v1"
```

## 5. Testar o script

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

Se tudo isso funcionou, pode seguir para configurar o Zabbix.

## 6. Instalar e configurar o Zabbix Agent

O agente precisa rodar **no mesmo servidor** onde estão o Python, o script e
o FlareSolverr, já que o `UserParameter` chama tudo localmente.

### 6.1. Instalar

Se este servidor já usa o repositório oficial do Zabbix, pule o `apt update`
de repositório e vá direto no `apt install`. Caso contrário, o pacote
`zabbix-agent2` já vem disponível direto pelos repositórios padrão do
Debian 13:

```bash
apt update
apt install -y zabbix-agent2
```

### 6.2. Configurar

Edite `/etc/zabbix/zabbix_agent2.conf`:

```ini
Server=<IP do seu Zabbix Server>
ServerActive=<IP do seu Zabbix Server>
Hostname=<nome do host cadastrado no Zabbix>
Timeout=30
```

### 6.3. Aplicar o UserParameter

```bash
mkdir -p /etc/zabbix/zabbix_agent2.d
cp /opt/downdetector-zabbix/zabbix/downdetector.conf /etc/zabbix/zabbix_agent2.d/downdetector.conf
```

Se os arquivos estão em `/opt/downdetector-zabbix`, o arquivo já vem com os
caminhos certos, não precisa editar nada. Se estão em outro lugar, abra o
arquivo copiado e ajuste os dois caminhos antes de reiniciar o agente:

```bash
nano /etc/zabbix/zabbix_agent2.d/downdetector.conf
```

Reinicie e habilite o serviço:

```bash
systemctl restart zabbix-agent2
systemctl enable zabbix-agent2
```

Conteúdo esperado do `zabbix/downdetector.conf`:

```ini
# Descoberta (LLD): lista todos os serviços da home (rápido, sem relatos).
UserParameter=downdetector.discovery,/usr/bin/python3 /caminho/downdetector_scraper.py --lld

# Item mestre: dados completos de UM serviço (logo, nome, status, relatos).
# $1 = slug do serviço, ex.: whatsapp, nubank, steam.
UserParameter=downdetector.status[*],/usr/bin/python3 /caminho/downdetector_scraper.py --service $1 --flat
```

### 6.4. Testar

```bash
zabbix_agent2 -t "downdetector.discovery"
zabbix_agent2 -t "downdetector.status[whatsapp]"
```

Se aparecer JSON válido nos dois casos, o agente está pronto. Se aparecer
vazio ou erro, veja a seção de [Troubleshooting](#8-troubleshooting).

## 7. Criar o Template no Zabbix

### 7.1. Criar o Host

`Data collection → Hosts → Create host`
- **Host name**: o mesmo definido em `Hostname=` no agente
- **Interfaces**: adicione o IP deste servidor (Agent, porta 10050)
- **Templates**: adicione o template criado no passo seguinte

### 7.2. Criar o Template com descoberta automática (LLD)

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

## 8. Troubleshooting

- **`docker: command not found`**: o Docker não foi instalado ou o terminal
  não recarregou o `PATH`. Rode `which docker`; se vazio, refaça o
  [passo 2](#2-instalar-o-docker).
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
- **`ModuleNotFoundError` ao rodar o script**: as dependências Python não
  foram instaladas para o usuário/interpretador que está executando. Refaça
  o [passo 3](#3-instalar-as-dependências-python) — se usar virtualenv,
  lembre de ajustar o caminho do Python no `UserParameter` (passo 6.3) para
  `/caminho/.venv/bin/python3`.
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
