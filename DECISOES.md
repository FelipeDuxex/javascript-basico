# Decisões técnicas

Registro das decisões tomadas de forma autônoma durante a construção, e o porquê
de cada uma. A ordem segue a importância prática, não a cronologia.

---

## 1. Detecção de blocos: `auto` = razão máxima, com Jenks como segunda opinião

**A decisão.** O método padrão (`--metodo-bloco auto`) funciona assim, dentro de
cada dia separadamente:

1. Descarta intervalos abaixo do piso (2 min) — eles nunca quebram bloco.
2. Ordena os intervalos candidatos e procura o **maior salto relativo**
   `gap[i+1]/gap[i]`. Se esse salto for ≥ 2,5x, o limiar é a **média geométrica**
   entre os dois intervalos do salto.
3. Se nenhum salto atinge 2,5x, tenta **Jenks natural breaks com 2 classes**,
   exigindo GVF ≥ 0,6 e razão entre as médias das classes ≥ 2,5x.
4. Se nenhum dos dois se convence, o dia é **um único bloco** — sem divisão
   inventada.

**Por que razão máxima primeiro.** Ela é escala-livre por construção, que é
exatamente a propriedade que falta ao limiar fixo: uma pausa de 20 min é enorme
num dia de takes de 2 min e irrelevante num dia de takes de 40 min. E o
resultado é explicável em uma linha na tela ("maior salto 21,8x entre intervalos
ordenados"), o que atende ao requisito de transparência muito melhor que "GVF
0,84".

**Por que Jenks como rede de segurança.** Medi os quatro métodos nos cenários
sintéticos e a razão máxima falha num caso real:

| método | cen. 2 (pausa 2h) | cen. 3 (pausa 20min) | cen. 4 (uniforme) | cen. 5 (semana) | cen. 6 (fragmentação) |
|---|---|---|---|---|---|
| esperado | 2 blocos | 2 blocos | 1 bloco | [3,2,4,2,3] | ≤ 6 blocos |
| **auto** | **2** ✓ | **2** ✓ | **1** ✓ | **[3,2,4,2,3]** ✓ | **6** ✓ |
| ratio | 2 ✓ | 2 ✓ | 1 ✓ | [3,2,4,2,3] ✓ | **1** ✗ |
| jenks | 2 ✓ | 2 ✓ | 1 ✓ | [3,2,4,2,3] ✓ | 6 ✓ |
| kmeans | 2 ✓ | 2 ✓ | 1 ✓ | [3,2,4,2,3] ✓ | 6 ✓ |
| fixo (30 min) | 2 ✓ | **1** ✗ | 1 ✓ | [3,2,4,2,3] ✓ | **1** ✗ |

No cenário 6 (muitos intervalos médios espalhados: takes a cada ~4 min, pausas
de 8 a 26 min) o maior salto consecutivo é de apenas **2,03x** — abaixo do mínimo
de 2,5x. A razão máxima então declara o dia uniforme e devolve 1 bloco, o que
está errado: há pausas reais ali. O Jenks separa as duas populações sem
depender de um salto abrupto entre vizinhos, acha 10 blocos, e o teto de 6
mescla os menores intervalos. Daí a composição: **razão máxima quando o salto é
inequívoco, Jenks quando o material é mais gradual.**

**Por que não Jenks sozinho.** Passa em todos os cenários, mas perde o principal
ativo da razão máxima: a justificativa legível. Como a interface precisa mostrar
o limiar decidido e permitir discordar dele, prefiro o método cuja decisão eu
consigo explicar sem falar de variância.

**k-means 1D também está implementado** (`--metodo-bloco kmeans`) e empata com
Jenks nos cenários. Não é o padrão porque, em 1D com valores ordenados, testar
todos os pontos de corte (que é o que o Jenks faz) já dá o **ótimo global** —
o k-means de Lloyd é uma aproximação iterativa do mesmo problema, sem vantagem
alguma aqui. Ficou disponível só para comparação.

**`fixo` existe de propósito, como contraprova.** O cenário 3 usa ele para
demonstrar em teste que um limiar fixo de 30 min transformaria dois blocos em um.

**Média geométrica, não aritmética.** Os intervalos de gravação se distribuem em
escala logarítmica (segundos, minutos, horas). A média geométrica de 4 min e 2h
é 17 min; a aritmética é 1h02 — perto demais da classe alta e frágil a qualquer
gap intermediário.

### Salvaguardas e seus valores

| Salvaguarda | Valor | Motivo |
|---|---|---|
| Piso mínimo de pausa | 120 s | abaixo disso é troca de lente, não pausa; sem o piso o dia vira dezenas de micro-blocos |
| Teto de blocos/dia | 6 | acima disso a visualização perde a utilidade, e coincide com o tamanho da paleta de blocos |
| Mínimo de clipes para dividir | 6 | com menos que isso a distribuição de intervalos não sustenta nenhuma estatística |
| Salto mínimo | 2,5x | calibrado nos cenários: 2,0x deixaria passar variação normal de ritmo, 3,0x perderia pausas curtas legítimas |
| Pausa única do dia | ≥ 15% do span do dia | quando há um só intervalo candidato não existe distribuição para comparar; a fração do dia é o critério escala-livre disponível |

O teto é aplicado **mesclando iterativamente pelos menores intervalos**, como
pedido. Além disso, quando a razão máxima aponta um corte que estouraria o teto,
o `auto` prefere o maior salto que ainda o respeite — cortar certo é melhor que
cortar demais e mesclar às cegas. As duas rotas existem e as duas são testadas.

### A regra crítica: dia antes de bloco

A detecção roda **dentro de cada dia**, nunca sobre a semana. Isso está
verificado por contraprova no cenário 5: rodando a mesma detecção sobre a semana
inteira como se fosse um dia só, os intervalos noturnos (12h+) dominam a
distribuição e o resultado colapsa para ~1 bloco por dia — todas as pausas
internas desaparecem.

---

## 2. Interpretação de horário: dois regimes, não um

Esta é a decisão que faz o resto funcionar. Todo clipe cai em um de dois regimes:

**Regime A — instante absoluto confiável (`tz_known = True`).** O metadado traz
offset de fuso explícito: `com.apple.quicktime.creationdate` do iPhone
(`2026-08-13T00:47:01-0300`) ou sidecar Sony com fuso. Convertemos para o fuso do
projeto e **não aplicamos offset nenhum**. Confiança `alta`.

**Regime B — relógio literal (`tz_known = False`).** O valor vem do `moov/mvhd`,
que a especificação diz ser UTC mas que as câmeras preenchem com o relógio
interno cru. Tratamos o valor como **hora de parede local** e somamos o offset
calibrado do dispositivo. Confiança `média` se calibrado, `baixa` se não.

O `mvhd` guarda os dois usos no mesmo campo (`mvhd_time_utc` e
`mvhd_time_literal`) justamente para não perder essa distinção. Interpretar o
"Z" da Sony literalmente é o erro que quebra tudo, e ele é silencioso.

**Todo o pipeline trabalha em hora de parede local, sem tzinfo.** É como a
produção pensa ("gravamos das 10 às 12") e evita que horário de verão apareça no
meio de um agrupamento. A conversão acontece uma vez, na entrada.

**O iPhone é sempre a referência, sem perguntar.** O cadastro de câmera não pede
para indicar qual dos dois clipes está certo: o de fuso explícito ganha por
definição. Se o clipe de referência não trouxer fuso, o app avisa em vez de
recusar — pode ser um iPhone lido por caminho degradado.

**Offset manual sobrepõe até fonte confiável.** O ajuste fino manual é aplicado
mesmo em clipes do regime A. Se o usuário mandou deslocar, ele mandou.

---

## 3. Leitura parcial: parser próprio de atoms em vez de `ffprobe`

`ffprobe` é a ferramenta certa em disco local e a errada no Google Drive: ele
abre o arquivo por uma API de I/O genérica e pode arrastar bem mais que o
cabeçalho. Com 340 GB de material, isso é inviável.

**Decisão: parser próprio, percorrendo a árvore de atoms por `seek()`.** Detalhes
que importam:

- **Estratégia híbrida por tamanho.** Container até 512 KB é lido de uma vez
  (1 seek + 1 read, mais rápido que muitos seeks pequenos). Acima disso — o
  `moov` de um clipe longo, onde as tabelas de amostra pesam MB — é percorrido
  por seek, e `mdat`/`stco`/`stsz`/`stts`/`stsc` **nunca são lidos**.
- **`moov` no início ou no fim.** Se não está entre os primeiros atoms, o leitor
  salta para o fim sem tocar no meio. Ambos os layouts são testados num arquivo
  esparso de 3 GB, medindo os bytes efetivamente lidos (menos de 10 KB nos dois).
- **`CountingReader` com teto.** Todo byte é contabilizado, e há um limite duro
  de 4 MB por arquivo. Se um arquivo exótico tentar estourar isso, ele cai para
  o `ffprobe` em vez de arrastar gigabytes silenciosamente.
- **`ffprobe` continua no projeto, como fallback explícito**, e cada uso é
  registrado como `read_method: ffprobe` no relatório, para saber quais arquivos
  custaram caro.

**Handler de mídia com lista branca.** O `minf` também tem um atom `hdlr` (o
*data* handler: `alis`, `url `). Sem filtrar por tipos de mídia conhecidos, ele
sobrescreve o `hdlr` do `mdia` e todo track de `.mov` do iPhone é classificado
como `url`. Bug encontrado no primeiro teste real do parser.

### Ordem completa de leitura

Da mais barata para a mais cara; a primeira que funcionar ganha:
`manifesto.json` → cache → sidecar XML → atoms MP4/MOV (ou `bext` de WAV) →
`ffprobe` → `mtime`.

---

## 4. WAV/BWF lido pelo chunk `bext` (adição não pedida, mas necessária)

O Hollyland grava WAV, e WAV não tem `moov`. Pelo caminho previsto no escopo,
todo arquivo de áudio externo cairia no fallback de `mtime` — que no Drive é
inútil, o que deixaria o áudio externo permanentemente sem horário confiável.

O padrão Broadcast Wave (EBU Tech 3285) resolve: o chunk `bext` guarda
`OriginationDate` e `OriginationTime` no cabeçalho do arquivo, e é o que
gravadores profissionais escrevem. **Implementei `wavreader.py` com a mesma
filosofia do parser de MP4**: percorre os chunks RIFF por `seek()`, lê `fmt `,
`bext` e `iXML`, e nunca toca no chunk `data`. Um WAV de 2 GB no Drive custa
~1 KB de leitura. Fallbacks internos: `iXML` (Zoom/Tascam/Sound Devices) e
`LIST/INFO ICRD`.

Sem isso, o áudio externo — que é justamente o que mais falta nos takes — seria o
elo fraco da organização.

---

## 5. Cache indexado por nome + tamanho, nunca por `mtime`

No Drive o `mtime` reflete a sincronização, não a gravação: muda sozinho, muda ao
trocar de máquina, muda ao recompartilhar. Usá-lo como chave de cache
invalidaria entradas boas a esmo, e é exatamente o cenário em que reler custa
caro.

**Chave = `nome_do_arquivo` (minúsculo) + `tamanho_em_bytes`.** A colisão exigiria
dois arquivos de mesmo nome e mesmo byte-count — e nesse caso o app já os reporta
como duplicata. Verificado por teste: alterar o `mtime` de arquivos já lidos não
invalida o cache.

**SQLite em vez de JSON.** É biblioteca padrão, escreve de forma atômica, aceita
acesso concorrente das threads de leitura e não precisa reserializar o arquivo
inteiro a cada `put`. Um JSON com centenas de entradas reescrito a cada arquivo
lido seria pior em tudo.

**O cache guarda o registro cru de metadado, não o `Clip` montado.** Assim,
recalibrar uma câmera, renomear um dispositivo ou mudar o fuso do projeto tem
efeito imediato **sem invalidar nada** — a identificação e a correção de relógio
são reaplicadas a cada organização, que é barato.

---

## 6. `mtime` desligado na nuvem, em vez de usado silenciosamente

Detectado volume de nuvem/rede (padrão no caminho, tipo de sistema de arquivos,
`DRIVE_REMOTE` no Windows), o fallback de `mtime` é **desligado**. O clipe vira
"data desconhecida" e fica fora da timeline, listado no relatório para resolução
manual.

Posicionar um clipe num horário inventado é pior que não posicionar: ele entra na
distribuição de intervalos e contamina a detecção de blocos do dia inteiro. Um
arquivo ausente é visível; um arquivo no lugar errado é invisível.

---

## 7. Interface: `http.server` da biblioteca padrão, sem Flask

Considerei Flask (rápido de escrever) e `customtkinter` (sem navegador).
Escolhi o `http.server` + uma página estática porque:

- **Zero dependências para instalar.** `python -m timeline_sync web` funciona
  numa máquina recém-formatada. Num app de uso pessoal que vou abrir de madrugada
  antes de editar, "não precisa de venv" vale mais que conveniência de código.
- O que Flask oferece — roteamento, templates, sessões — não é usado aqui: são
  ~15 endpoints JSON e uma página. `ThreadingHTTPServer` cobre o caso.
- Timeline horizontal com blocos coloridos e zoom é HTML/CSS trivial e desenho
  manual em `customtkinter`.

Escuta apenas em `127.0.0.1`. Sem autenticação, porque não há nada para
autenticar: é um processo local lendo arquivos locais.

**Separação caro/barato na sessão.** `scan()` é caro (toca a mídia) e roda uma
vez; `rebuild()` é barato (só reagrupa clipes já lidos) e roda a cada ajuste de
parâmetro. É isso que permite arrastar o slider de limiar e ver a timeline se
reorganizar na hora. A varredura roda em thread com progresso e cancelamento;
cancelar não perde trabalho, porque o que já foi lido está no cache.

**A prévia usa a mesma função de cor da exportação** (`colors.label_for`),
chamada no backend. Reimplementar a lógica de cor em JavaScript garantiria
divergência entre o que aparece na tela e o que aparece no Premiere.

---

## 8. XML montado como texto, e não com ElementTree

O `xmeml` v4 é verboso mas simples, e não existe biblioteca confiável para ele.
Montar como texto com um acumulador indentado deixa o código legível na ordem em
que o formato espera os elementos — que é significativa em partes do schema. Com
`ElementTree` o mesmo código ficaria em construção de nós, mais longe do
documento final e mais difícil de conferir contra um XML que o Premiere aceita.

Escape de conteúdo via `xml.sax.saxutils.escape`, e `pathurl` via
`urllib.parse.quote`. Todos os XMLs gerados nos cenários são reparseados com
`ElementTree` no teste, o que garante que continuam bem formados.

Decisões de conteúdo:

- **Um arquivo XML por dia, mais um MASTER.** Uma semana numa timeline só é
  ingerível de editar. Cada arquivo é autocontido (traz seus próprios bins e
  `<file>`), então importar um dia isolado funciona.
- **Uma trilha de áudio por dispositivo**, não uma trilha de áudio compartilhada.
  Se o áudio da Sony e o do Hollyland caem na mesma trilha, o ganho de ter o
  Hollyland separado se perde.
- **Vídeo e áudio do mesmo arquivo ligados por `<link>`**, para mover o clipe no
  Premiere levar o áudio junto.
- **`<masterclipid>` + bins** para o painel de projeto espelhar `DIA / BLOCO`.
- **`mastercomment1..3`** carregam dia/bloco, dispositivo + hora real, e método de
  leitura + confiança. Ficam visíveis em colunas no Premiere, o que transforma o
  painel de projeto num relatório navegável.

---

## 9. Cores: a cor é o bloco, a posição é o aparelho

O Premiere tem 16 labels nomeadas e ponto. Numa semana com 14+ blocos, "uma cor
por bloco" não fecha.

**A cor representa o bloco dentro do dia, e a paleta reinicia a cada dia.** Como
cada dia sai em sua própria sequência, não há ambiguidade — e com teto de 6
blocos/dia a paleta de 6 cores nunca se esgota. As 6 escolhidas (Cerulean,
Mango, Forest, Rose, Iris, Caribbean) são bem separadas entre si e contra o fundo
escuro, e a ordem garante que blocos consecutivos nunca se repitam.

**O dispositivo fica na posição vertical** (V1, V2, V3, trilhas de áudio). Cor =
quando, posição = qual aparelho: duas dimensões independentes, nenhuma disputa
pela mesma.

Os hex no app são **aproximações** das labels do Premiere, usadas só na prévia —
o que atravessa para o Premiere é o nome, e ele pinta com os valores dele. O
teste verifica que todo `<label2>` gerado está na lista de 16 válidas.

**Cor de marcador: limitação documentada.** O xmeml não tem campo de cor de
marcador que o Premiere respeite. A cor do bloco vai no comentário do marcador.

---

## 10. Takes: dispositivo repetido abre take novo

Dentro de um bloco, clipes que começam dentro da janela (10 s) formam um take —
**exceto** quando o dispositivo já está no take. Se a Sony gravou dois arquivos
em 10 segundos, são duas tomadas (ou uma gravação dividida), não a mesma tomada
capturada duas vezes pelo mesmo aparelho.

Sem essa regra, um dia com a Sony gravando em rajada colapsaria vários takes num
só e o relatório de takes órfãos perderia sentido.

---

## 11. Tempo morto: mapa de tempo aplicado a todo vazio, não só entre blocos

`TimeMap` traduz hora real → segundos na timeline por uma função linear por
trechos, com um ajuste por vazio encurtado. Aplica-se a **todo** intervalo acima
do mínimo configurado (5 min), não apenas às fronteiras de bloco: um vazio de 40
min dentro de um bloco incomoda tanto quanto um entre blocos.

Padrão `compress` a 10% em vez de `close`: um resto de vazio proporcional
preserva a sensação de ritmo da diária, que ajuda a se localizar na timeline.
`preserve` e `close` estão disponíveis. Na MASTER, os dias são separados por 5 s
fixos para as fronteiras ficarem visíveis.

---

## 12. Sanidade de relógio como alerta destacado

Duas verificações automáticas, ambas para o mesmo erro silencioso (offset errado
ou relógio zerado no meio da produção):

1. Dispositivo que é o **único** presente em algum dia, enquanto outros aparelhos
   gravaram em outros dias.
2. Dispositivo cujo intervalo de datas **não intersecta** o de nenhum outro.

O segundo é o mais forte: se a Sony aparece numa semana inteiramente diferente de
todo mundo, o offset está errado, sem exceção prática.

---

## 13. Executável: PyInstaller, dois formatos, e o ffmpeg de fora

**O `.exe` sai do CI, não da máquina de desenvolvimento.** O PyInstaller não faz
cross-compile — ele empacota o interpretador do sistema em que roda. Para sair
um executável de Windows é preciso um Windows, e é isso que
`.github/workflows/build-exe.yml` faz. `build_exe.py` roda em qualquer sistema e
serve para validar o empacotamento em si (recursos embutidos, imports
dinâmicos, inicialização) antes de gastar uma rodada de CI.

**Dois formatos, porque eles falham de jeitos diferentes.** `onefile` é o que as
pessoas esperam de um `.exe`, mas inicia mais devagar (descompacta num
diretório temporário toda vez) e é o formato que mais atrai falso-positivo de
antivírus. `onedir` num `.zip` inicia rápido e passa com menos atrito. Publicar
os dois custa quase nada e dá um plano B real quando o antivírus come o
primeiro.

**`build_exe.py` verifica o binário de verdade, não só se ele existe.** O modo
de falha típico do PyInstaller não é falhar o build — é gerar o executável e ele
morrer no primeiro import dinâmico, ou não achar os arquivos da interface. Então
a verificação roda o binário: `--versao`, `dispositivos`, `cache`, uma
organização completa com exportação de XML, e sobe o servidor buscando `/`,
`/static/style.css`, `/static/app.js` e `/api/estado`. Sem isso, "build passou"
não significaria nada.

**`ffmpeg` não vai embutido.** São dezenas de MB e licença própria, e o app não
precisa dele: os parsers próprios cobrem MP4, MOV e WAV, que é todo o material.
Ele só habilita o refino por áudio. Em vez de embutir ou exigir PATH,
`runtime.find_tool()` procura **ao lado do executável** primeiro (e em
`ferramentas/`, `bin/`, `ffmpeg/bin/`) e só depois no PATH — largar
`ffmpeg.exe` na pasta do app basta. A resolução acontece a cada chamada, não na
importação, então dá para largar o binário com o app já aberto. A interface
desabilita o botão de refino e explica o motivo quando ele não está presente.

**`numpy` também fica de fora — mas só depois de eu tornar isso verdade.** A
versão original em Python puro fazia busca exaustiva: ~24 milhões de
multiplicações, dezenas de segundos por par de clipes. Isso tornava o `numpy`
obrigatório na prática. Reescrevi a correlação em duas passadas (varredura sobre
os sinais decimados por 8, depois ajuste fino em volta do resultado): mesma
resposta, tempo comparável ao `numpy`. Com isso o executável economiza ~30 MB e
uma fonte conhecida de problema no empacotamento, e quem roda do código-fonte
com `numpy` instalado continua usando o caminho vetorizado.

**`tzdata` vai embutido.** O Windows não tem base de fusos do sistema; sem ela o
`zoneinfo` falha e o app cai no `-03:00` fixo. Isso está correto para São Paulo
hoje (o Brasil acabou com o horário de verão em 2019), mas quebraria qualquer
outro fuso — e o fallback silencioso é justamente o tipo de erro que este
projeto inteiro tenta evitar.

**Console visível, não janela oculta.** `--console` em vez de `--windowed`
porque o console é onde aparecem a URL, a barra de progresso e os erros. Num app
sem janela, um erro de inicialização vira "o programa não abre". Pelo mesmo
motivo o launcher segura a janela com um `input()` quando morre por exceção.

**Duplo-clique abre a interface.** Ninguém que clicou num `.exe` quer digitar
`web` depois. Sem argumentos o launcher sobe o servidor e abre o navegador; com
argumentos ele se comporta exatamente como a CLI. E se a porta 8730 estiver
ocupada, tenta as 20 seguintes em vez de morrer com um traceback.

**Ícone gerado em Python puro** (`build/gerar_icone.py`), sem Pillow: o desenho é
o próprio assunto do app (trilhas horizontais com clipes coloridos, nas mesmas
cores da paleta de blocos). Escrevi como ICO com entradas BMP em vez de
PNG-dentro-de-ICO porque BMP é lido sem ressalva por qualquer parser; PNG em ICO
só vale de Vista pra frente e depende de quem está interpretando.

---

## 14. Saída de console forçada para UTF-8

O primeiro build de Windows falhou na verificação com `UnicodeEncodeError:
'charmap' codec can't encode character '→'`. O console do Windows usa
cp1252 por padrão, e cp1252 não tem `→` — que aparece em toda a saída deste app
("relógio marcava X → hora real Y", as notas de detecção de bloco, o display dos
perfis). Isso derrubava `dispositivos`, `organizar` e `calibrar` no meio da
execução — e não só no executável: **rodando do código-fonte num Windows dava o
mesmo erro.**

Duas medidas, porque uma só não basta:

- `SetConsoleOutputCP(65001)` põe o console em UTF-8, para o texto *aparecer*
  certo;
- `reconfigure(errors="replace")` garante que, se ainda assim algum caractere não
  couber (redirecionamento para arquivo, terminal antigo), ele vire `?` em vez de
  abortar o comando. Perder um símbolo é aceitável; perder a execução inteira,
  não.

A alternativa seria trocar `→` por `->` em toda a saída. Rejeitei: o problema
não é o caractere, é a saída não estar configurada — e o próximo símbolo fora do
cp1252 traria o bug de volta.

`PYTHONIOENCODING=cp1252` reproduz o erro exatamente no Linux, então a regressão
está guardada por teste sem precisar de um Windows.

**Este bug é o argumento a favor de verificar o binário de verdade.** O build
passou, o `.exe` foi gerado, o `--versao` respondeu. Se a verificação parasse aí,
o executável teria sido publicado quebrando no primeiro comando útil.

---

## 15. Dois defeitos no refino por áudio, encontrados ao medir

Achados ao comparar os caminhos `numpy` e Python puro durante o trabalho de
empacotamento. Ambos corrigidos, ambos com teste de regressão.

**As duas implementações discordavam.** O caminho `numpy` usava
`np.correlate` sem normalizar pela sobreposição; o Python puro dividia pela
contagem de amostras sobrepostas. Dividir favorece deslocamentos extremos, onde
poucas amostras se encontram e a média sobe por acaso — foi assim que apareceu
um `-6,485s` entre dois clipes. Agora os dois usam a soma crua normalizada pelas
energias globais, e a faixa de deslocamentos é limitada aos que mantêm pelo
menos 50% de sobreposição.

**A função respondia sobre ruído.** Dados dois áudios sem transiente nenhum
(zumbido uniforme), ela devolvia um número com aparência de resultado. Não há
evento em comum para alinhar ali: a resposta é ruído, e um refino baseado em
ruído é pior que nenhum refino. Agora `cross_correlate` exige razão pico/média
≥ 2,0 em pelo menos um dos sinais e devolve `None` caso contrário — a mesma
guarda que `peak_offset` já tinha. Medido no material de teste: os clipes de
calibração com palma dão 26,85 de razão pico/média, os tons planos dão 1,45.

---

## 15. Escolhas menores

**Português nos nomes de comando, atributos e interface**, com inglês onde é
termo técnico consagrado (`timebase`, `pathurl`, `label`). É um app de uso
pessoal; o vocabulário é o da produção.

**Virada de dia às 4h por padrão.** Gravação que varou até 01:30 pertence ao dia
anterior. Meia-noite fixa parte a diária em duas, e a contraprova disso está no
cenário 7.

**6 workers de leitura.** O gargalo é rede, não CPU, então vale passar de
`n_cpus`; passar muito além disso faz o Drive serializar as requisições e a taxa
cai. Configurável.

**Duração assumida de 5 s quando não foi lida**, com aviso no clipe. Um clipe sem
duração desapareceria da timeline; um clipe com duração aproximada e marcado é
recuperável.

**Sidecar Sony: `C0020M01.XML`, `C0020.XML`, e o espelho `SUB/` → `CLIP/`.** Com
parser XML tolerante e fallback por regex, porque sidecar truncado por
sincronização interrompida do Drive é uma ocorrência real. Para DJI, o `.SRT`
(telemetria) e o `.LRF` (proxy) **não** trazem hora de relógio confiável, então
não são usados; quando existe um XML no padrão NRT ele é lido pelo mesmo caminho
genérico.

**Refino por áudio via envelope de energia, não correlação de forma de onda
crua.** Envelope é robusto à diferença de microfone e ganho entre um iPhone e um
Hollyland; a fase da onda não sobrevive a essa diferença. `numpy` acelera quando
está disponível, e há uma implementação em Python puro para quando não está.

**O refino por áudio sugere, não aplica.** Ele devolve os ajustes propostos e a
decisão de aplicar é do usuário. É refinamento opcional, e o método principal
continua sendo metadado.

**`peak_offset` recusa-se a responder sem transiente claro** (pico < 3x a média
do envelope). Em áudio ambiente uniforme, "o instante mais alto" é ruído — e um
refino baseado em ruído é pior que nenhum refino. Por isso a calibração do
cenário 1 gera uma palma de verdade nos clipes de cadastro.

**Ajustes manuais de bloco são chaveados pelo instante ISO do clipe que abre o
bloco**, não por índice. Índices mudam quando a detecção muda; o instante
sobrevive a mudança de limiar, e é o que permite reabrir o projeto semanas depois
com os ajustes intactos.

**`gerar_testes.py` injeta atoms na mão.** O `ffmpeg` normaliza `creation_time`
para UTC e descarta as chaves Apple, então material gerado só com `ffmpeg` não
reproduziria nem o bug da Sony nem o acerto do iPhone — validaria o caminho
fácil e deixaria passar o difícil. `mp4write.py` escreve `moov/udta` estilo
QuickTime, `moov/meta` estilo Apple (hdlr + keys + ilst) e corrige `stco`/`co64`
quando o `moov` cresce antes do `mdat`. Ele é material de teste, não parte do
fluxo do app — o app é estritamente somente-leitura sobre a mídia.
