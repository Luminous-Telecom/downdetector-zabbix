# Plano de implementação: Scraper Downdetector Brasil (BR)

> Documento de arquitetura e plano do scraper contra **https://downdetector.com.br/**.  
> A implementação atual usa **FlareSolverr + JSDOM** (sem Puppeteer na app) e API HTTP autenticada por `API_TOKEN`.

---

## 1. Objetivo

API/serviço local que:

1. **Resumo da homepage**: coleta `https://downdetector.com.br/`, extrai o grid de serviços em tendência (nome, slug, status).
2. **Detalhe do serviço**: coleta `https://downdetector.com.br/fora-do-ar/<slug>/` e extrai status, pico 24h, e o **valor atual de relatos** (último ponto de `dataPoints`).

Requisitos:
- Contornar Cloudflare (via FlareSolverr).
- Textos e status em **português (BR)**.
- Horários em **Brasília** (`America/Sao_Paulo`).
- Endpoints `/api/*` protegidos por token.

---

## 2. Origem e evolução

Base inicial: [oTaKaTo/downdetector-scraper](https://github.com/oTaKaTo/downdetector-scraper) por [Takdanai Deephuak (oTaKaTo)](https://github.com/oTaKaTo).

Adaptações para o BR:

| Antes (TH) | Agora (BR) |
|---|---|
| `th.downdetector.com/en/` | `downdetector.com.br/` |
| `/en/status/<slug>` | `/fora-do-ar/<slug>/` |
| Status em inglês | `sem problemas` / `possíveis problemas` / `problemas` |
| Puppeteer + Chrome local | FlareSolverr + parse HTML (JSDOM) |
| Sem auth | `API_TOKEN` obrigatório em `/api/*` |
| Fuso Bangkok | Fuso Brasília |

---

## 3. Fluxo de alto nível

```
┌──────────────────────────────────────────────────────────────┐
│ 1. Cron (~15 min) ou request HTTP autenticado                │
│ 2. POST FlareSolverr → HTML de downdetector.com.br           │
│ 3. Parse DOM (JSDOM) / extrair dataPoints do payload Next.js │
│ 4. Normalizar status → OK | WARNING | DOWN                   │
│ 5. Cache em memória (summary) / service sem cache (TTL=0)    │
│ 6. (Opcional) Diff de alertas → D1 → Teams                   │
└──────────────────────────────────────────────────────────────┘
```

Pontos de entrada:
- **API HTTP** (`app.js`): `GET /api/summary`, `GET /api/service/:slug`, etc.
- **Cron** em background: refresh da homepage.

---

## 4. Estrutura DOM (downdetector.com.br)

Preferir `aria-label`, `role` e `data-testid` — **não** depender de classes Tailwind.

### 4a. Homepage — grid de serviços

```html
<ul class="contents" aria-label="Lista de 48 serviços">
  <li>
    <div data-testid="card-company-33191" ...>
      <a aria-label="Página de status Caixa Econômica Federal"
         href="/fora-do-ar/caixa/"></a>
      <div role="img"
           aria-label="Relatos de Caixa Econômica Federal nas últimas 24 horas. Status atual: Possíveis problemas">
      </div>
    </div>
  </li>
</ul>
```

Extração:
1. `ul.contents` com `aria-label` contendo `serviç` / `service`.
2. Cada `div[data-testid^="card-company-"]`:
   - **companyId**: sufixo numérico de `data-testid`.
   - **slug**: de `a[href*="/fora-do-ar/"]` → `/fora-do-ar/caixa/` → `caixa`.
   - **nome**: `aria-label` do link sem o prefixo `Página de status `.
   - **status bruto**: regex em `Status atual:\s*(.+)$`.

### 4b. Página do serviço — gráfico 24h

```html
<div data-testid="card">
  <div role="img"
       aria-label="Gráfico de relatos das últimas 24 horas com pico de 65 relatos, status: Possíveis problemas">
  </div>
</div>
```

Extração:
1. `div[role="img"][aria-label*="Gráfico"]`.
2. Regex: `pico de (\d+) relatos?,\s*status:\s*(.+)$`.
3. **Valor atual**: parsear o JSON escapado `\"dataPoints\":[...]` no HTML (payload Next.js) e usar o **último** ponto:
   - `reportsValue` → `reports`
   - `baselineValue` → `reportsBaseline`
   - `timestampUtc` → `reportsAt` (formatado em Brasília)

### 4c. Parsing defensivo
- Null-check por campo.
- Não hardcodar classes Tailwind.
- Se FlareSolverr ainda devolver challenge Cloudflare, falhar com erro claro (ou retry).

---

## 5. Cloudflare / coleta

No BR, Puppeteer headless falha com frequência no Turnstile. Estratégia adotada:

1. **FlareSolverr** como único coletor (`FLARESOLVERR_URL`).
2. Parse do HTML com **JSDOM** (`homepage.js`, `service.js`).
3. Cron com **jitter ±60s** para não bater sempre no mesmo segundo.
4. Cache do summary (~15 min); service com `SERVICE_CACHE_TTL_MS=0` (sempre fresco).
5. `?refresh=1` força nova coleta.

Proxy opcional pode ser configurado no próprio FlareSolverr, se necessário.

---

## 6. Schema de dados

### Homepage — `/api/summary`
```json
{
  "fetchedAt": "21/07/2026, 6:20 PM",
  "totalServicesListed": 48,
  "services": [
    {
      "companyId": "33191",
      "name": "Caixa Econômica Federal",
      "slug": "caixa",
      "status": "WARNING",
      "rawStatus": "Possíveis problemas"
    }
  ],
  "stale": false
}
```

### Serviço — `/api/service/caixa`
```json
{
  "fetchedAt": "21/07/2026, 6:20 PM",
  "slug": "caixa",
  "name": "Caixa Econômica Federal",
  "status": "WARNING",
  "rawStatus": "Possíveis problemas",
  "reports": 34,
  "reportsBaseline": 4,
  "reportsAt": "21/07/2026, 6:05 PM",
  "peakReports24h": 70,
  "stale": false
}
```

Normalização de status (PT-BR):

| Texto no site | Enum |
|---|---|
| `sem problemas` | `OK` |
| `possíveis problemas` / `possiveis...` | `WARNING` |
| `problemas` (sem “possível”) | `DOWN` |

---

## 7. Estrutura do projeto

```
downdetector/
├── app.js                 ← HTTP + cron + auth
├── src/
│   ├── flaresolverr.js    ← cliente FlareSolverr
│   ├── homepage.js        ← parse homepage BR
│   ├── service.js         ← parse /fora-do-ar/<slug>/ + dataPoints
│   ├── statusUtils.js     ← OK / WARNING / DOWN
│   ├── timeBr.js          ← formatação Brasília
│   ├── cache.js           ← cache TTL
│   ├── statusDiff.js      ← diff de incidentes
│   ├── d1Client.js        ← Cloudflare D1 (opcional)
│   ├── notifier.js        ← Teams / Power Automate (opcional)
│   ├── r2Uploader.js      ← R2 (opcional)
│   └── config/
│       └── services.json  ← bancos, telecom, gov, etc.
├── .env
├── .env.example
├── package.json
└── README.md
```

Lista inicial em `services.json`: Caixa, BB, Bradesco, Itaú, Santander, Nubank, Inter, Neon, PicPay, Sicoob, Pix, Vivo, Claro, TIM, WhatsApp, Instagram, Facebook, Google, YouTube, Mercado Livre, GOV.BR, Sefaz, Dataprev, Correios.

---

## 8. Tarefas (já implementadas / checklist)

1. [x] Cliente FlareSolverr (`src/flaresolverr.js`)
2. [x] `homepage.js` para `downdetector.com.br` (aria PT-BR + `/fora-do-ar/`)
3. [x] `service.js` com pico + **reports atual** via `dataPoints`
4. [x] `statusUtils.js` com vocabulário PT-BR
5. [x] `timeBr.js` (horário Brasília, estilo 12h do site)
6. [x] `app.js` sem Puppeteer; cron + endpoints
7. [x] Auth `API_TOKEN` (Bearer / `X-API-Token` / `?token=`)
8. [x] Cache configurável + `?refresh=1`
9. [x] `services.json` com serviços BR
10. [x] README em português
11. [ ] D1 / Teams / R2 — opcionais, só se credenciais no `.env`

---

## 9. Checklist de validação

- [ ] `/api/summary` lista ~48 serviços e inclui `caixa`
- [ ] Status de amostra batem com o site (OK / WARNING / DOWN)
- [ ] `/api/service/caixa` retorna `reports` = último ponto do gráfico (não só o pico)
- [ ] `reportsAt` em horário de Brasília, alinhado ao tooltip do site
- [ ] Sem token → `401`
- [ ] Com token → `200`
- [ ] `?refresh=1` busca HTML novo via FlareSolverr
- [ ] FlareSolverr fora do ar → erro claro, sem crash silencioso

---

## 10. API rápida

| Método | Caminho | Auth | Descrição |
|---|---|---|---|
| `GET` | `/` | Não | Help + status do cron |
| `GET` | `/api/services` | Sim | Lista de `services.json` |
| `GET` | `/api/summary` | Sim | Resumo homepage |
| `GET` | `/api/service/:slug` | Sim | Detalhe (ex.: `caixa`) |
| `GET` | `/api/alerts` | Sim | Histórico D1 (se configurado) |

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  http://localhost:3333/api/service/caixa
```

---

## 11. Variáveis de ambiente

| Variável | Obrigatório | Descrição |
|---|---|---|
| `API_TOKEN` | Sim | Token dos endpoints `/api/*` |
| `FLARESOLVERR_URL` | Sim | Ex.: `http://127.0.0.1:8191/v1` |
| `PORT` | Não | Padrão `3333` |
| `SUMMARY_INTERVAL_MS` | Não | Cron homepage (padrão 15 min) |
| `CACHE_TTL_MS` | Não | Cache do summary |
| `SERVICE_CACHE_TTL_MS` | Não | `0` = sempre fresco no detail |
| `CLOUDFLARE_*` / `D1_*` | Não | Persistência de alertas |
| `POWER_AUTOMATE_WEBHOOK_URL` | Não | Notificação Teams |
| `R2_*` | Não | Screenshots (legado/opcional) |

---

## 12. Decisões de desenho

| Decisão | Motivo |
|---|---|
| Só FlareSolverr | Mais estável no BR contra Turnstile/Cloudflare |
| Sem `crawler-profile` | Perfil Chrome não é mais necessário |
| `API_TOKEN` | Evita uso anônimo da API |
| `dataPoints` para valor atual | O aria do gráfico só traz o **pico**; o atual está no último ponto |
| Fuso Brasília | Igual à UX do site BR |
| Service sem cache (`TTL=0`) | Site atualiza com frequência; evita dado “velho” |

---

## 13. Entregável

Repositório Node.js funcional apontando para **downdetector.com.br**, com README e este plano alinhados ao código. Se o DOM do site mudar, revalidar seletores/`dataPoints` no HTML ao vivo antes de ajustar o parser.
