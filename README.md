# youtube-automation-news
Sistema automático de geração de vídeos de notícias

## Ajustes de pós-produção (config.json)

Chaves opcionais — se ausentes, o pipeline usa o comportamento antigo (nada quebra):

| Chave | O que faz | Padrão |
|---|---|---|
| `fonte_destaque_arquivo` | Caminho de um `.ttf` no repositório pra usar na palavra-destaque (ex: `"fonts/RoadRage-Regular.ttf"`) | usa a mesma fonte da legenda |
| `fonte_destaque_divisor` | Controla o tamanho da palavra-destaque: `tamanho = largura_do_vídeo / divisor`. Número **menor** = texto **maior** | `8` |
| `fonte_destaque_tamanho_px` | Tamanho em pixels direto, ignora o divisor acima se definido | `null` (usa o divisor) |
| `antecipacao_sfx_transicao` | Segundos que o SFX de transição (woosh) toca ANTES do instante teórico do corte, pra soar no meio do efeito visual em vez de atrasado | `0.25` |
| `transicoes_video` | Lista de transições a sortear entre blocos — inclui as 4 built-in (`crossfade`, `flash`, `glitch`, `shadow_wipe`) mais qualquer arquivo colocado em `assets/transicoes/` (ver abaixo) | `["crossfade"]` |
| `portal_transparencia_token` | Token gratuito do Portal da Transparência, usado por `fonte_dados.py`. Cadastre em https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email | `""` (fonte fica desativada sem isso) |
| `modo_roteiro` | `"cadeia_completa"` (tese/objeção), `"simples"` (devocional) ou **`"capitulos_webdoc"`** (formato investigativo em capítulos, tipo Elementar — ver seção abaixo) | `"cadeia_completa"` |
| `num_capitulos_webdoc` | Quantos capítulos gerar no modo `capitulos_webdoc` | `3` |
| `duracao_card_capitulo` | Segundos que o card preto de transição fica na tela | `2.2` |

## Modo webdoc em capítulos (`modo_roteiro: "capitulos_webdoc"`)

Formato investigativo tipo Elementar: introdução com dado forte → capítulos nomeados
(cada um com card preto de transição + música tema própria) → desfecho.

```json
{
  "modo_roteiro": "capitulos_webdoc",
  "num_capitulos_webdoc": 3
}
```

Como funciona por baixo (sem exigir nenhuma mudança no pipeline de TTS/áudio):
- O roteiro é gerado com um título curto por capítulo (ex: "A Fábrica de Prédios"), e
  esse título entra **falado** no início do próprio bloco de narração.
- Como o resto do pipeline já mapeia o timestamp de cada bloco pela contagem de
  palavras do Whisper, o instante em que o narrador começa a dizer o título do
  capítulo já é conhecido de graça — é ali que o card preto (`gerar_card_capitulo`)
  aparece, por cima do B-roll, com fade in/out.
- A cada capítulo, `assets/musicas/` sorteia uma faixa **diferente** (com crossfade de
  1,5s na troca) em vez de uma faixa única tocando o vídeo inteiro — dá pra colocar
  várias faixas de clima diferente ali (uma mais tensa, uma mais séria, etc.) e o
  sistema distribui uma por capítulo automaticamente.

Sem essa chave no `config.json` (ou com o modo padrão), nada muda — é 100% opt-in.


## Fontes de dados públicas (fonte_dados.py)

Módulo plugável pra alimentar roteiros de webdoc investigativo com dado real e
citável — cruza IBGE (população), Câmara dos Deputados (leis/gastos de
parlamentar) e Portal da Transparência (repasses federais por município), sem
custo nenhum.

```python
from fonte_dados import montar_pauta_municipio

pauta = montar_pauta_municipio("Mariana", "MG", ano=2024)
for fato in pauta["fatos"]:
    print(f"{fato['texto']}  [{fato['fonte']}]")
```

- **IBGE**: 100% grátis, sem chave, já funciona direto.
- **Câmara dos Deputados**: 100% grátis, sem chave (`camara_buscar_deputado`,
  `camara_gastos_deputado`, `camara_buscar_proposicoes`).
- **Portal da Transparência**: precisa de `portal_transparencia_token` no
  `config.json` (cadastro gratuito por e-mail, ver tabela acima) — sem ele,
  essa fonte específica fica desligada e o resto do módulo continua normal.

Todo fato retornado vem com o campo `fonte` — use isso no roteiro pra citar de
onde veio cada número (fortalece a credibilidade do vídeo, além de ser mais
fácil de auditar antes de publicar).


## Transições customizadas (assets/transicoes/)

O sistema de transições é plugável por arquivo, não só por código: qualquer vídeo
`.mp4`/`.mov`/`.webm` colocado em `assets/transicoes/` vira automaticamente uma opção
de transição, usando o **nome do arquivo** (sem extensão) como identificador.

Funciona no formato **luma matte** (padrão de mercado — é como os packs de transição do
Premiere/DaVinci/CapCut funcionam): o vídeo precisa ser em preto-e-branco, onde **branco
= mídia nova aparece** e **preto = mídia antiga continua visível**. Não precisa de canal
alpha nem codec especial.

Onde baixar packs gratuitos (sem marca d'água, procure por "free luma matte transition
pack"): Mixkit, Videezy e Pixabay Videos costumam ter vários. Baixe, jogue os `.mp4`
dentro de `assets/transicoes/`, e adicione o nome do arquivo em `transicoes_video` no
`config.json`:

```json
{
  "transicoes_video": ["crossfade", "meu_wipe_organico", "meu_zoom_diagonal"]
}
```

Se um arquivo específico falhar ao renderizar, o pipeline cai pro `crossfade` automaticamente
sem derrubar o vídeo inteiro (mesmo comportamento de segurança das outras transições).
