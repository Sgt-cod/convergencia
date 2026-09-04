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
