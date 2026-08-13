# Timeline Sync

Organiza automaticamente os arquivos de vídeo e áudio de uma gravação de vários
dias e gera timelines prontas para importar no **Adobe Premiere Pro** — cada
dispositivo na sua própria track, os clipes posicionados pelo horário real de
gravação, e coloração que deixa a estrutura do material legível de relance.

Feito para um caso de uso específico: **minissérie vertical gravada ao longo de
uma semana com Sony a7III + iPhone + DJI + gravador Hollyland, com o material
armazenado no Google Drive e editado em modo streaming.**

- Uso pessoal e local. Sem login, sem nuvem, sem multiusuário, sem billing.
- **Nenhum arquivo de vídeo sai da máquina.** O app só lê metadado, e apenas
  alguns KB por arquivo.
- **Somente-leitura sobre a mídia.** Nunca modifica, move ou renomeia os
  originais. Todo output vai para arquivos novos.

```
DIA (data de gravação)
 └── BLOCO (janela contínua, separada por pausas)
      └── TAKE (clipes de dispositivos diferentes no mesmo momento)
           └── CLIPE (arquivo individual)
```

---

## Antes de tudo: confira o relógio da Sony

A a7III foi comprada nos EUA. Câmeras Sony gravam no `creation_time` o valor
**literal do relógio interno** e rotulam como UTC (`...Z`) **sem converter fuso
de verdade**. Se o relógio não foi reconfigurado, o horário sai errado por horas
inteiras — e o sufixo "Z" é enganoso.

O app resolve isso por calibração (abaixo), mas **acertar o relógio da câmera
antes de gravar economiza um passo e evita erro acumulado**. A bateria interna
descarregando também zera o relógio no meio de uma produção — quando isso
acontece, é só recalibrar.

O iPhone nunca precisa de calibração: ele acerta a hora sozinho via rede/GPS e
grava o fuso explicitamente. **Ele é sempre a referência de hora.**

---

## Instalação

Requer **Python 3.9+**. Não há dependência obrigatória para instalar — a
interface roda no servidor HTTP da biblioteca padrão.

```bash
git clone https://github.com/FelipeDuxex/timeline-sync.git
cd timeline-sync
python -m timeline_sync web        # abre a interface em http://127.0.0.1:8730
```

Opcionais:

| Ferramenta | Para quê | Sem ela |
|---|---|---|
| `ffmpeg` / `ffprobe` | fallback de leitura, refino por áudio, gerar material de teste | o parser próprio cobre MP4/MOV/WAV; refino por áudio fica indisponível |
| `numpy` | acelera a correlação cruzada de áudio | usa uma versão mais lenta em Python puro |

```bash
# Ubuntu/Debian
sudo apt install ffmpeg
# macOS
brew install ffmpeg
# opcional
pip install numpy
```

---

## Fluxo recomendado (Google Drive)

O material fica no Drive e é editado em streaming, sem download. Uma leitura
ingênua de metadado faria o Drive baixar arquivos inteiros de 4K — horas de
espera e banda desperdiçada. O caminho mais rápido é **gerar o manifesto
enquanto o material ainda está no cartão**:

```bash
# 1. cartão ainda na máquina (disco físico, leitura rápida)
python -m timeline_sync manifesto /Volumes/CARTAO_SONY

# 2. suba a pasta para o Drive COM o manifesto.json dentro

# 3. daí em diante, o app lê só o manifesto — zero I/O na mídia
python -m timeline_sync web
```

Sem manifesto o app continua funcionando: ele lê apenas o cabeçalho de cada
arquivo (dezenas de KB) e guarda em cache permanente. A diferença é que a
primeira passada custa uma leitura por arquivo, e o manifesto custa zero.

---

## Uso

### Interface (recomendado)

```bash
python -m timeline_sync web
python -m timeline_sync web --porta 9000 --projeto minisserie
```

Numa tela só: apontar as pastas, ver os dias detectados, a timeline por
dispositivo, ajustar o limiar de blocos com slider e ver a reorganização na
hora, mesclar/dividir blocos na mão, clicar num clipe e ver o metadado bruto, e
exportar.

### Linha de comando

```bash
# organiza e exporta (um XML por dia + MASTER)
python -m timeline_sync organizar "/Volumes/GoogleDrive/Meu Drive/SEMANA_01" \
    --saida saida --projeto minisserie

# só alguns dias
python -m timeline_sync organizar PASTA --dias 2026-08-13,2026-08-15

# só analisar, sem gerar XML
python -m timeline_sync organizar PASTA --sem-exportar

# cadastrar/recalibrar uma câmera contra o iPhone
python -m timeline_sync calibrar C0020.MP4 IMG_0431.MOV

# ver/editar perfis salvos
python -m timeline_sync dispositivos
python -m timeline_sync dispositivos --renomear "sony:ILCE-7M3=Sony A"
python -m timeline_sync dispositivos --offset "sony:ILCE-7M3=-2"

# manifesto, cache, projetos
python -m timeline_sync manifesto /Volumes/CARTAO
python -m timeline_sync cache
python -m timeline_sync projetos
```

`python -m timeline_sync organizar --help` lista todos os parâmetros de
organização (fuso, virada de dia, método de blocos, teto, janela de take, tempo
morto, cores, resolução, workers...).

---

## Cadastro de dispositivo (correção de relógio)

Uma vez por câmera:

1. Grave alguns segundos com a câmera **e** com o iPhone ao mesmo tempo. Uma
   palma no meio ajuda, mas não é obrigatório.
2. `python -m timeline_sync calibrar CLIPE_DA_CAMERA CLIPE_DO_IPHONE`

O app compara os `creation_time`, assume o iPhone como correto e salva o offset
em `~/.timeline_sync/dispositivos.json`. **Não é preciso ter começado a gravar
no mesmo instante** — alguns segundos de diferença são irrelevantes frente a um
erro de horas. Se os dois clipes tiverem áudio e houver um transiente claro
(palma/claquete), o app localiza o pico em cada um e zera também o erro de
segundos.

Nas importações seguintes o dispositivo é reconhecido pelo metadado e o offset é
aplicado automaticamente. Câmera não cadastrada gera aviso destacado, mas não
bloqueia.

Existe também um **offset manual em segundos** por dispositivo, como ajuste fino
e válvula de escape.

### Sanidade automática

Se, depois de aplicar os offsets, um dispositivo aparecer em dias onde ninguém
mais gravou — ou com datas que não cruzam com as de nenhum outro aparelho — o
app **alerta em destaque**. Isso é quase sempre offset errado ou relógio zerado
no meio da produção, e é o tipo de erro que passa despercebido e estraga a
organização inteira.

---

## Detecção de blocos

Sem limiar fixo. O ritmo varia entre dias e produções: uma diária tem pausa de
20 min entre blocos, outra tem 2 horas, outra troca de setup em 8 minutos.

**A regra crítica: primeiro separa por dia, depois detecta blocos dentro de cada
dia.** Se a detecção rodasse sobre a semana inteira, os intervalos noturnos
(12h+) dominariam a estatística e o algoritmo se limitaria a separar por dia,
ignorando as pausas internas de cada diária. Isso é verificado por teste.

Dentro de cada dia, os intervalos são medidos na **linha do tempo consolidada de
todos os dispositivos juntos** — se a Sony parou mas o iPhone continuou
gravando, não houve pausa real. O corte sai da distribuição desses intervalos
(ver `DECISOES.md` para a comparação entre os métodos).

Salvaguardas:

| Salvaguarda | Padrão | Por quê |
|---|---|---|
| Piso mínimo de pausa | 2 min | senão o dia vira dezenas de micro-blocos |
| Teto de blocos por dia | 6 | blocos demais destroem a utilidade da visualização |
| Dia uniforme → 1 bloco | — | não inventa divisão onde não há pausa |
| Dia com poucos clipes → 1 bloco | < 6 clipes | amostra pequena demais para estatística |

O limiar decidido para cada dia aparece na tela e no relatório
("Dia 03: blocos detectados com pausa mínima de 47 min"). Dá para ajustar com
slider por dia, aplicar um limiar a todos os dias de uma vez, ou mesclar e
dividir blocos na mão. **Esses ajustes ficam salvos por projeto**, então reabrir
o material da mesma produção depois não perde nada.

---

## Cores

O Premiere usa um conjunto **fixo de 16 labels nomeadas**. Numa semana com 14+
blocos as cores acabam — por isso a estratégia é pensada, não "uma cor nova por
bloco".

**Padrão: a cor representa o BLOCO dentro do dia, e a paleta reinicia a cada
dia.** O primeiro bloco de todo dia tem a mesma cor, o segundo a mesma cor, e
assim por diante. Como cada dia sai em sua própria sequência, não há
ambiguidade — e com teto de 6 blocos/dia as cores nunca acabam. Blocos
consecutivos nunca recebem a mesma cor.

**O dispositivo continua identificável pela posição vertical** (Sony na V1,
iPhone na V2, DJI na V3, Hollyland na track de áudio).
**Cor = quando. Posição = qual aparelho.** As duas informações coexistem.

Modos alternativos: **colorir por dia** (útil na MASTER) e **colorir por
dispositivo** (conferência rápida de cobertura). A prévia na tela usa exatamente
a mesma função de cor da exportação.

---

## Exportação

Formato **Final Cut Pro XML 7** (`xmeml` v4), que o Premiere importa
nativamente.

- **Uma sequência por dia** (`DIA 01 — 2026-08-13`), em um arquivo por dia. Uma
  semana inteira numa única timeline fica ingerível de editar.
- **Uma sequência MASTER** opcional com todos os dias em ordem, para visão geral.
- Uma track de vídeo por dispositivo, e uma track de áudio por dispositivo (o
  áudio da Sony nunca se mistura com o do Hollyland).
- Marcadores por bloco: `BLOCO 01 — 10:03 às 12:14 (2h11, 34 clipes)`. Na
  MASTER, também `=== DIA 02 — 2026-08-14 ===`.
- Bins no painel de projeto espelhando `DIA 01 / BLOCO 02`.
- Exportar tudo, um intervalo de dias, ou um dia só.

### Tempo morto

Três modos: preservar o tempo real, comprimir proporcionalmente (padrão, 10%),
ou fechar totalmente. Vazios menores que o mínimo configurado ficam intactos.

### Se o Drive montar em outra letra

Os `pathurl` do xmeml precisam ser absolutos. Para não ter que fazer relink de
centenas de clipes no Premiere quando o `G:` virar `H:`, a interface tem o campo
**raiz da mídia**: troque a raiz e reexporte.

```bash
python -m timeline_sync organizar PASTA \
    --raiz-midia "G:/Meu Drive/MINISSERIE" \
    --nova-raiz-midia "H:/Meu Drive/MINISSERIE"
```

---

## Como o app lê os metadados

Da estratégia mais barata para a mais cara. A primeira que funcionar ganha:

| # | Estratégia | Custo típico por arquivo |
|---|---|---|
| 1 | `manifesto.json` na pasta | **0 bytes de mídia** |
| 2 | cache local (`~/.timeline_sync/`) | **0 bytes de mídia** |
| 3 | sidecar XML da Sony (`C0020M01.XML`) | ~1 KB |
| 4 | parser próprio de atoms MP4/MOV | ~5–60 KB |
| 4 | chunk BWF `bext` de WAV | ~1 KB |
| 5 | `ffprobe` | pode arrastar muito mais |
| 6 | `mtime` do arquivo | 0, mas **desligado na nuvem** |

O parser próprio percorre a árvore de atoms com `seek()` e lê só `moov/mvhd`,
`moov/udta` e `moov/meta`. Nunca toca no `mdat` nem nas tabelas de amostra
(`stco`, `stsz`, `stts`), que são as partes grandes. O `moov` pode estar no
início (faststart) ou no fim — se não está entre os primeiros atoms, o leitor
salta direto para o fim, sem ler o meio.

Isso é medido: **num arquivo sintético de 3 GB, o leitor toca menos de 10 KB** —
verificado nos dois layouts de `moov`. O relatório final registra a métrica real
("14 MB efetivamente transferidos de 340 GB totais").

O cache é indexado por **nome do arquivo + tamanho em bytes**, deliberadamente
**não** por `mtime` — no Drive o `mtime` reflete a sincronização, muda sozinho e
invalidaria o cache a esmo.

Quando a pasta está num volume de nuvem/rede, o app avisa e **desliga o fallback
de `mtime`**: em vez de posicionar o clipe num horário inventado e contaminar a
detecção de blocos, marca como "data desconhecida" para resolução manual.

---

## Relatório

Sai na tela e como `relatorio.txt` junto dos XMLs:

```
=== RESUMO DA IMPORTACAO ===
187 arquivos lidos | 4 dispositivos | 5 dias | 14 blocos | 96 takes
Leitura parcial: 4.1 MB efetivamente transferidos de 340.2 GB totais (0.0012%)
3 arquivos sem creation_time (usaram fallback de data de modificacao)

DIA 01 | 2026-08-13 | 3 blocos | 41 clipes
  BLOCO 01 | 10:03-12:14 | 2h11 | 34 clipes | Sony(20) iPhone(14)
  ...
  Limiar de pausa detectado (auto/ratio): 47 min
```

Inclui também: dispositivos e estado de calibração, arquivos com fallback,
arquivos sem data, **takes órfãos** (só um dispositivo gravou — geralmente falta
de áudio externo), duplicatas, avisos de sanidade e a configuração usada.

---

## Material de teste

Nenhum arquivo real é usado para validar: material que não é do mesmo dia não
prova nada sobre agrupamento.

```bash
python gerar_testes.py            # gera os 8 cenários (vídeos leves, metadado realista)
python gerar_testes.py --listar
python gerar_testes.py -c 3 5     # só alguns
python gerar_testes.py --escala 0.5   # metade dos arquivos

python rodar_cenarios.py          # roda o pipeline e valida tudo
```

Os vídeos são 320x180 de poucos segundos, mas o **metadado é realista**: Sony com
relógio literal rotulado UTC, iPhone com `com.apple.quicktime.creationdate` e
fuso explícito, DJI com seu próprio erro de relógio, Hollyland em WAV com chunk
BWF `bext`. O `ffmpeg` normaliza `creation_time` e descarta as chaves Apple, por
isso os atoms são injetados na mão (`timeline_sync/mp4write.py`).

Cenários validados:

| # | Cenário | O que prova |
|---|---|---|
| 1 | Fuso errado (Sony 3h adiantada) | a calibração contra o iPhone corrige, e o refino por áudio zera os segundos |
| 2 | Blocos óbvios (pausa de 2h) | exatamente 2 blocos |
| 3 | Pausa curta (~20 min) | ainda 2 blocos — com contraprova de que limiar fixo de 30 min daria 1 |
| 4 | Dia contínuo (3h uniformes) | 1 bloco, sem divisão inventada |
| 5 | Semana completa (5 dias) | separação por dia antes dos blocos, cores reiniciando, com contraprova da regra crítica |
| 6 | Fragmentação excessiva | teto de 6 blocos/dia acionado, mesclando pelos menores intervalos |
| 7 | Virada de madrugada (22h→01:30) | um dia só — com contraprova da virada à meia-noite |
| 8 | Metadado ausente | fallback sinalizado, execução inteira, e "data desconhecida" no modo nuvem |

Mais cinco verificações: leitura parcial em arquivo de 3 GB, modo manifesto,
cache com chave sem `mtime`, os três modos de tempo morto, e troca da raiz da
mídia no XML.

O log completo fica em `exemplos/log_cenarios.txt`, e os XMLs de exemplo (um por
cenário, incluindo a semana completa com MASTER) em `exemplos/`.

---

## Onde ficam os arquivos

```
~/.timeline_sync/
├── dispositivos.json          perfis de câmera e offsets calibrados
├── cache_metadados.sqlite     cache permanente (nome + tamanho)
└── projetos/<nome>.json       config e ajustes manuais de bloco por projeto
```

Nada disso contém vídeo — só metadado e preferências. Apagar é seguro: o app
relê o que precisar.

---

## Limitações conhecidas

- **Cor de marcador**: o xmeml não tem campo de cor de marcador que o Premiere
  respeite. A cor do bloco vai no comentário do marcador.
- **Caminhos relativos**: o Premiere não importa `pathurl` relativo de forma
  confiável, então os caminhos são absolutos. O campo "raiz da mídia" existe
  justamente para contornar isso.
- **Bins**: o Premiere importa a hierarquia de bins do xmeml, mas o comportamento
  varia entre versões. Se os bins não aparecerem, as sequências continuam
  completas e corretas.
- **`ffprobe` na nuvem**: quando o parser próprio falha e o `ffprobe` entra, o
  arquivo específico demora mais. O app avisa quais foram.
- **DaVinci Resolve**: o FCPXML 7 é aceito na prática, mas não foi testado aqui.
- **Refino por forma de onda**: é refinamento opcional, não o método principal.
  Ele sugere ajustes; aplicar é decisão sua.

---

## Fora de escopo

Contas, login, multiusuário, upload para nuvem, billing, sync por waveform como
método principal, transcodificação, proxies e correção de cor. O app não toca no
conteúdo do vídeo.
