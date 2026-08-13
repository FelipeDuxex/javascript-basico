# XMLs de exemplo

Saída real de `python rodar_cenarios.py` — um conjunto por cenário de teste,
mais `log_cenarios.txt` com o resultado de cada verificação.

| Pasta | O que tem dentro |
|---|---|
| `01_fuso_errado/` | 1 dia, 3 blocos — depois da calibração da Sony contra o iPhone |
| `02_blocos_obvios/` | 1 dia, 2 blocos, com trilhas de áudio separadas (Sony / iPhone / Hollyland) |
| `03_pausa_curta/` | 1 dia, 2 blocos, pausa de ~20 min detectada sem limiar fixo |
| `04_dia_continuo/` | 1 dia, 1 bloco — nenhuma divisão inventada |
| `05_semana_completa/` | **5 XMLs (um por dia) + `MASTER_todos_os_dias.xml`** — 300 clipes, 14 blocos, 4 dispositivos |
| `06_fragmentacao/` | 1 dia com o teto de 6 blocos acionado |
| `07_virada_madrugada/` | 22h→01:30 num único dia |
| `08_metadado_ausente/` | um clipe de fallback marcado nos comentários |

Cada pasta traz também o `relatorio.txt` daquela organização.

## Para abrir no Premiere

Os `pathurl` dentro dos XMLs apontam para os arquivos do material sintético na
máquina onde foram gerados. Para que o Premiere encontre a mídia, gere o
material e reexporte localmente:

```bash
python gerar_testes.py        # cria material_teste/ (~200 MB)
python rodar_cenarios.py      # reescreve estes XMLs com os caminhos locais
```

Sem isso os XMLs ainda **importam** (a estrutura de sequências, trilhas, cores e
marcadores aparece inteira), mas o Premiere vai pedir relink da mídia. Serve bem
para conferir o formato; para ver clipes de verdade, gere o material primeiro.

O começo de `05_semana_completa/MASTER_todos_os_dias.xml` é o melhor lugar para
ver a estrutura completa: bins `DIA / BLOCO`, marcadores de dia e de bloco,
labels de cor e trilhas por dispositivo.
