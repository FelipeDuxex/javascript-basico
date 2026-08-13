/* Timeline Sync — interface local.
   A previa usa exatamente as cores que a exportacao usa (vem do backend). */

const $ = (id) => document.getElementById(id);
let estado = null;
let diaAtual = null;
let clipeSelecionado = null;
let pollTimer = null;

/* ---------------- utilidades ---------------- */

async function api(rota, corpo) {
  const opcoes = corpo
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(corpo) }
    : {};
  const resp = await fetch(rota, opcoes);
  const texto = await resp.text();
  try { return JSON.parse(texto); } catch (e) { return { erro: texto }; }
}

function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h${String(m).padStart(2, '0')}`;
  if (m) return `${m}min`;
  return `${s}s`;
}

function fmtGap(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h${String(m).padStart(2, '0')}min`;
  if (m) return `${m} min`;
  return `${s} s`;
}

function fmtBytes(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = Math.max(0, n || 0), i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${u[i]}`;
}

function hora(iso) {
  if (!iso) return '--:--';
  return iso.slice(11, 16);
}

function mostraAvisos(lista, classe) {
  const alvo = $('avisos');
  (lista || []).forEach((texto) => {
    const div = document.createElement('div');
    div.className = 'aviso' + (classe ? ' ' + classe : '');
    div.textContent = texto;
    alvo.appendChild(div);
  });
}

function limpaAvisos() { $('avisos').innerHTML = ''; }

/* ---------------- ciclo de estado ---------------- */

async function carregaEstado() {
  estado = await api('/api/estado');
  aplicaEstado();
}

function aplicaEstado() {
  if (!estado) return;
  if (estado.state) {
    $('projeto-nome').textContent = estado.state.name || 'projeto';
    if (estado.state.folders && estado.state.folders.length && !$('pastas').value.trim()) {
      $('pastas').value = estado.state.folders.join('\n');
    }
  }
  $('info-cache').textContent = `cache: ${estado.cache_count ?? 0} arquivos`;
  const vol = estado.volume || {};
  const pv = $('info-volume');
  if (vol.is_cloud) {
    pv.textContent = `nuvem: ${vol.provider}`;
    pv.classList.add('nuvem');
  } else {
    pv.textContent = 'volume local';
    pv.classList.remove('nuvem');
  }

  preencheConfig(estado.config || {});
  desenhaDispositivos();
  desenhaDias();
  desenhaExportacao();

  const p = estado.project;
  if (p) {
    const pct = p.total_bytes ? (p.bytes_read / p.total_bytes * 100) : 0;
    $('resumo-topo').textContent =
      `${p.total_files} arquivos · ${p.devices.length} dispositivos · ` +
      `${p.days.length} dias · ${p.block_count} blocos · ${p.take_count} takes · ` +
      `${fmtBytes(p.bytes_read)} lidos de ${fmtBytes(p.total_bytes)} (${pct.toFixed(3)}%)`;
    limpaAvisos();
    mostraAvisos(p.warnings);
    if (!diaAtual && p.days.length) diaAtual = p.days[0].key;
    if (diaAtual && !p.days.some((d) => d.key === diaAtual)) {
      diaAtual = p.days.length ? p.days[0].key : null;
    }
    desenhaDia();
  }
}

/* ---------------- configuracao ---------------- */

const CAMPOS = {
  'cfg-timezone': 'timezone',
  'cfg-day-start': 'day_start_hour',
  'cfg-block-method': 'block_method',
  'cfg-min-gap': 'block_min_gap_seconds',
  'cfg-max-blocks': 'block_max_per_day',
  'cfg-min-ratio': 'block_min_ratio',
  'cfg-min-clips': 'block_min_clips_for_split',
  'cfg-take-window': 'take_window_seconds',
  'cfg-gap-mode': 'gap_mode',
  'cfg-gap-factor': 'gap_compress_factor',
  'cfg-gap-min': 'gap_min_to_treat_seconds',
  'cfg-color-mode': 'color_mode',
  'cfg-width': 'sequence_width',
  'cfg-height': 'sequence_height',
  'cfg-timebase': 'sequence_timebase',
  'cfg-workers': 'workers',
  'cfg-root-orig': 'media_root_original',
  'cfg-root-novo': 'media_root_override',
};
const CHECKS = {
  'cfg-ffprobe': 'use_ffprobe_fallback',
  'cfg-mtime': 'use_mtime_fallback',
};

const PALETAS = {
  bloco: ['#4a8fd4', '#d9922e', '#3f7a45', '#d97f96', '#6f6bc8', '#3f9e93'],
  dia: ['#8f7fc4', '#2e8f8f', '#bd9a6a', '#3f5fa8', '#54a054', '#b03f8c', '#c9bf3f'],
  dispositivo: ['#3f5fa8', '#d9922e', '#54a054', '#b03f8c', '#4a8fd4', '#bd9a6a',
                '#d97f96', '#2e8f8f'],
};

function preencheConfig(cfg) {
  for (const [id, campo] of Object.entries(CAMPOS)) {
    const el = $(id);
    if (el && cfg[campo] !== undefined && document.activeElement !== el) {
      el.value = cfg[campo];
    }
  }
  for (const [id, campo] of Object.entries(CHECKS)) {
    const el = $(id);
    if (el && cfg[campo] !== undefined) el.checked = !!cfg[campo];
  }
  $('exp-master').checked = !!cfg.export_master;
  const paleta = PALETAS[cfg.color_mode] || PALETAS.bloco;
  $('paleta').innerHTML = paleta
    .map((c) => `<span style="background:${c}"></span>`).join('');
}

async function salvaConfig() {
  const corpo = {};
  for (const [id, campo] of Object.entries(CAMPOS)) {
    const el = $(id);
    if (el) corpo[campo] = el.value;
  }
  for (const [id, campo] of Object.entries(CHECKS)) {
    const el = $(id);
    if (el) corpo[campo] = el.checked;
  }
  corpo.export_master = $('exp-master').checked;
  estado = await api('/api/config', corpo);
  aplicaEstado();
}

/* ---------------- dias ---------------- */

function desenhaDias() {
  const lista = $('lista-dias');
  lista.innerHTML = '';
  const p = estado.project;
  if (!p) return;
  p.days.forEach((dia) => {
    const li = document.createElement('li');
    if (dia.key === diaAtual) li.classList.add('ativo');
    const cores = PALETAS.bloco;
    const barras = dia.blocks
      .map((b, i) => `<span style="background:${cores[i % cores.length]}"></span>`)
      .join('');
    li.innerHTML =
      `<div class="dia-nome">${dia.label} <span class="dia-meta">${dia.key}</span></div>` +
      `<div class="dia-meta">${dia.blocks.length} bloco(s) · ${dia.clip_count} clipes</div>` +
      `<div class="barrinhas">${barras}</div>`;
    li.onclick = () => { diaAtual = dia.key; desenhaDias(); desenhaDia(); };
    lista.appendChild(li);
  });
}

function diaCorrente() {
  const p = estado && estado.project;
  if (!p) return null;
  return p.days.find((d) => d.key === diaAtual) || null;
}

function timelineCorrente() {
  if (!estado || !estado.timelines) return null;
  return estado.timelines.find((t) => t.day === diaAtual) || null;
}

/* ---------------- timeline ---------------- */

function desenhaDia() {
  const dia = diaCorrente();
  const tl = timelineCorrente();
  const alvo = $('timeline');
  if (!dia || !tl) {
    alvo.innerHTML = '<p class="vazio">Nenhum dia carregado.</p>';
    $('titulo-dia').textContent = 'Nenhum dia carregado';
    $('info-dia').textContent = '';
    $('blocos-lista').innerHTML = '';
    return;
  }

  $('titulo-dia').textContent = tl.sequence_name;
  const limiar = dia.threshold_seconds
    ? `limiar ${fmtGap(dia.threshold_seconds)} (${dia.threshold_source})`
    : `bloco único (${dia.threshold_source})`;
  $('info-dia').textContent =
    `${dia.blocks.length} bloco(s) · ${dia.clip_count} clipes · ` +
    `duração da timeline ${fmtDur(tl.duration)} · ${limiar}` +
    (tl.removed_seconds ? ` · ${fmtDur(tl.removed_seconds)} de tempo morto encurtado` : '');

  const slider = $('limiar-dia');
  if (dia.threshold_seconds) slider.value = Math.round(dia.threshold_seconds);
  $('limiar-dia-valor').textContent = dia.threshold_seconds
    ? fmtGap(dia.threshold_seconds) : 'auto';

  const px = Number($('zoom').value) / 60;      // pixels por segundo
  const largura = Math.max(tl.duration * px, 400);
  const layout = (estado.layout && estado.layout.slots) || [];

  const html = [];
  html.push('<div class="tl-corpo" style="min-width:' + (largura + 120) + 'px">');

  // regua de tempo
  html.push('<div class="tl-regua" style="width:' + largura + 'px">');
  const passo = escolhePasso(tl.duration);
  for (let t = 0; t <= tl.duration; t += passo) {
    html.push(`<div class="marca" style="left:${t * px}px">${fmtRelogio(tl, t)}</div>`);
  }
  html.push('</div>');

  // marcadores de bloco
  html.push('<div class="tl-marcadores" style="width:' + largura + 'px">');
  tl.markers.forEach((m) => {
    html.push(
      `<div class="tl-marcador" style="left:${m.at * px}px;background:${m.hex}" ` +
      `title="${escapa(m.name)} — ${escapa(m.comment)}">${escapa(m.name)}</div>`
    );
  });
  html.push('</div>');

  // uma linha por trilha
  const linhas = [];
  layout.forEach((slot) => {
    if (slot.video_index) linhas.push({ slot, tipo: 'video' });
  });
  layout.forEach((slot) => {
    if (slot.audio_index) linhas.push({ slot, tipo: 'audio' });
  });

  linhas.forEach(({ slot, tipo }) => {
    const via = tipo === 'video' ? 'V' + slot.video_index : 'A' + slot.audio_index;
    html.push('<div class="tl-linha">');
    html.push(`<div class="tl-rotulo" title="${escapa(slot.label)}">` +
              `<span class="via">${via}</span> ${escapa(slot.label)}</div>`);
    html.push(`<div class="tl-faixa" style="width:${largura}px">`);
    tl.clips.forEach((c, i) => {
      if (c.device_key !== slot.device_key) return;
      if (tipo === 'video' && !c.has_video) return;
      if (tipo === 'audio' && !c.has_audio) return;
      const l = c.start * px;
      const w = Math.max(c.duration * px, 3);
      const classes = ['tl-clipe'];
      if (tipo === 'audio') classes.push('audio');
      if (c.fallback) classes.push('fallback');
      if (clipeSelecionado === c.path + '|' + i) classes.push('selecionado');
      html.push(
        `<div class="${classes.join(' ')}" data-idx="${i}" ` +
        `style="left:${l}px;width:${w}px;background:${c.hex}" ` +
        `title="${escapa(c.filename)} — ${escapa(c.device_label)} — ` +
        `${(c.real_start || '').slice(11, 19)} — bloco ${c.block_index} take ${c.take_index}">` +
        `${escapa(c.filename)}</div>`
      );
    });
    // fronteiras de bloco
    tl.markers.filter((m) => m.kind === 'bloco').forEach((m) => {
      if (m.at <= 0.001) return;
      html.push(`<div class="tl-fronteira" style="left:${m.at * px}px"></div>`);
    });
    html.push('</div></div>');
  });
  html.push('</div>');
  alvo.innerHTML = html.join('');

  alvo.querySelectorAll('.tl-clipe').forEach((el) => {
    el.onclick = () => {
      const idx = Number(el.dataset.idx);
      clipeSelecionado = tl.clips[idx].path + '|' + idx;
      mostraClipe(tl.clips[idx]);
      trocaAba('clipe');
      desenhaDia();
    };
  });

  desenhaBlocos(dia, tl);
}

function escolhePasso(duracao) {
  const alvos = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  for (const a of alvos) if (duracao / a <= 14) return a;
  return 14400;
}

function fmtRelogio(tl, segundos) {
  // A regua mostra a hora real do primeiro clipe + o deslocamento na timeline.
  const base = tl.clips.length ? tl.clips[0].real_start : null;
  if (!base) return fmtDur(segundos);
  const d = new Date(base);
  d.setSeconds(d.getSeconds() + segundos);
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0');
}

function desenhaBlocos(dia, tl) {
  const alvo = $('blocos-lista');
  const cores = PALETAS.bloco;
  const html = [];
  html.push(`<div class="dica">${escapa(dia.detection_note || '')}</div>`);
  dia.blocks.forEach((b, i) => {
    const devs = Object.entries(b.device_counts)
      .map(([k, v]) => `${k}(${v})`).join(' ');
    html.push(
      '<div class="bloco-card">' +
      `<span class="cor" style="background:${cores[i % cores.length]}"></span>` +
      `<span class="nome">${b.label}</span>` +
      `<span class="meta">${hora(b.start)}–${hora(b.end)} · ${fmtDur(b.duration)} · ` +
      `${b.clip_count} clipes · ${b.take_count} takes · ${escapa(devs)}</span>` +
      '<span class="acoes-bloco">' +
      (i > 0 ? `<button class="ghost small" data-mesclar="${b.index}">mesclar com anterior</button>` : '') +
      `<button class="ghost small" data-dividir="${b.index}">dividir aqui…</button>` +
      '</span></div>'
    );
  });
  alvo.innerHTML = html.join('');

  alvo.querySelectorAll('[data-mesclar]').forEach((el) => {
    el.onclick = async () => {
      estado = await api('/api/bloco/mesclar',
                         { dia: dia.key, bloco: Number(el.dataset.mesclar) });
      aplicaEstado();
    };
  });
  alvo.querySelectorAll('[data-dividir]').forEach((el) => {
    el.onclick = () => abreDividir(dia, Number(el.dataset.dividir));
  });
}

function abreDividir(dia, blocoIndex) {
  const bloco = dia.blocks[blocoIndex - 1];
  if (!bloco) return;
  const takes = bloco.takes || [];
  const opcoes = takes.map((t, i) =>
    `${i + 1}) ${(t.start || '').slice(11, 19)} — ${t.clips.join(', ')}`).join('\n');
  const escolha = prompt(
    `Dividir ${bloco.label} a partir de qual take?\n\n${opcoes}\n\nDigite o número:`);
  const n = Number(escolha);
  if (!n || n < 2 || n > takes.length) return;
  api('/api/bloco/dividir', { dia: dia.key, em: takes[n - 1].start })
    .then((novo) => { estado = novo; aplicaEstado(); });
}

/* ---------------- detalhe do clipe ---------------- */

function mostraClipe(c) {
  const linhas = [
    ['arquivo', c.filename],
    ['caminho', c.path],
    ['dispositivo', `${c.device_label} (${c.device_key})`],
    ['horário original', c.raw_time || '—'],
    ['fonte do horário', c.time_source],
    ['offset aplicado', `${(c.offset_seconds || 0).toFixed(0)} s`],
    ['horário corrigido', c.real_start || '—'],
    ['confiança', c.trust + (c.fallback ? ' (FALLBACK)' : '')],
    ['duração', `${(c.duration || 0).toFixed(2)} s`],
    ['bloco / take', `${c.block_index} / ${c.take_index}`],
    ['cor da label', c.color],
    ['trilha', (c.video_index ? 'V' + c.video_index : '') +
               (c.audio_index ? ' A' + c.audio_index : '')],
    ['codec', `${c.codec || '—'} ${c.width || 0}x${c.height || 0}`],
    ['tamanho', fmtBytes(c.size)],
    ['lido via', c.read_method],
  ];
  if (c.warnings && c.warnings.length) linhas.push(['avisos', c.warnings.join(' | ')]);
  $('detalhe-clipe').innerHTML = linhas
    .map(([k, v]) => `<dt>${escapa(k)}</dt><dd>${escapa(String(v))}</dd>`).join('');
}

/* ---------------- dispositivos ---------------- */

function desenhaDispositivos() {
  const alvo = $('lista-dispositivos');
  const perfis = estado.profiles || [];
  const contagens = {};
  const status = {};
  ((estado.project && estado.project.devices) || []).forEach((d) => {
    contagens[d.key] = d.clip_count;
    status[d.key] = d;
  });
  if (!perfis.length) {
    alvo.innerHTML = '<p class="dica">Nenhum perfil ainda. Varra uma pasta ou calibre uma câmera.</p>';
    return;
  }
  alvo.innerHTML = perfis.map((p) => {
    const cor = PALETAS.dispositivo[Object.keys(contagens).indexOf(p.key) % 8] || '#666';
    const resumo = status[p.key];
    const referencia = resumo && resumo.is_reference;
    const calibrado = referencia
      ? 'referência de hora (fuso explícito)'
      : (p.calibrated_at ? `calibrado ${p.calibrated_at.slice(0, 10)}` : 'NÃO CALIBRADO');
    const classe = (referencia || p.calibrated_at) ? 'ok' : 'alerta';
    return (
      '<div class="disp-card">' +
      `<div class="titulo"><span class="cor" style="background:${cor}"></span>` +
      `<strong>${escapa(p.label || p.key)}</strong>` +
      `<span class="chave">${contagens[p.key] || 0} clipes</span></div>` +
      `<div class="chave">${escapa(p.key)}</div>` +
      `<div class="estado ${classe}">offset ${fmtOffset(p.total_offset)} · ${calibrado}</div>` +
      '<div class="campos">' +
      `<input type="text" value="${escapa(p.label || '')}" data-apelido="${escapa(p.key)}" placeholder="apelido">` +
      `<input type="number" value="${p.manual_offset_seconds || 0}" data-offset="${escapa(p.key)}" title="ajuste fino em segundos" style="max-width:80px">` +
      `<button class="ghost small" data-apagar="${escapa(p.key)}">×</button>` +
      '</div></div>'
    );
  }).join('');

  alvo.querySelectorAll('[data-apelido]').forEach((el) => {
    el.onchange = async () => {
      estado = await api('/api/dispositivo',
                         { chave: el.dataset.apelido, apelido: el.value });
      aplicaEstado();
    };
  });
  alvo.querySelectorAll('[data-offset]').forEach((el) => {
    el.onchange = async () => {
      estado = await api('/api/dispositivo',
                         { chave: el.dataset.offset, offset_manual: Number(el.value) });
      aplicaEstado();
    };
  });
  alvo.querySelectorAll('[data-apagar]').forEach((el) => {
    el.onclick = async () => {
      if (!confirm('Apagar o perfil ' + el.dataset.apagar + '?')) return;
      estado = await api('/api/dispositivo', { chave: el.dataset.apagar, apagar: true });
      aplicaEstado();
    };
  });
}

function fmtOffset(s) {
  s = s || 0;
  const sinal = s < 0 ? '-' : '+';
  const t = Math.round(Math.abs(s));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), seg = t % 60;
  if (h) return `${sinal}${h}h${String(m).padStart(2, '0')}min${String(seg).padStart(2, '0')}s`;
  if (m) return `${sinal}${m}min${String(seg).padStart(2, '0')}s`;
  return `${sinal}${seg}s`;
}

/* ---------------- exportacao ---------------- */

function desenhaExportacao() {
  const p = estado.project;
  const alvo = $('exp-dias');
  if (!p || !p.days.length) {
    alvo.innerHTML = '<p class="dica">Nada para exportar ainda.</p>';
    return;
  }
  alvo.innerHTML = p.days.map((d) =>
    `<label><input type="checkbox" class="exp-dia" value="${d.key}" checked> ` +
    `${d.label} — ${d.key} (${d.blocks.length} bl., ${d.clip_count} cl.)</label>`
  ).join('');
}

/* ---------------- progresso ---------------- */

function iniciaPoll() {
  if (pollTimer) return;
  $('progresso').classList.remove('oculto');
  $('btn-varrer').disabled = true;
  $('btn-cancelar').disabled = false;
  pollTimer = setInterval(async () => {
    const pr = await api('/api/progresso');
    const pct = pr.pct || 0;
    $('progresso-preenche').style.width = pct + '%';
    $('progresso-texto').textContent =
      `${pr.done || 0}/${pr.total || 0} · ${fmtBytes(pr.bytes_read)} lidos · ` +
      `ETA ${Math.round(pr.eta || 0)}s · ${pr.current || ''}`;
    if (!pr.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      $('btn-varrer').disabled = false;
      $('btn-cancelar').disabled = true;
      $('progresso').classList.add('oculto');
      limpaAvisos();
      if (pr.error) mostraAvisos([pr.error], 'erro');
      if (pr.cancelled) mostraAvisos(['varredura cancelada — o que já foi lido ficou no cache'], 'erro');
      if (pr.message) mostraAvisos([pr.message]);
      await carregaEstado();
    }
  }, 400);
}

/* ---------------- eventos ---------------- */

function trocaAba(nome) {
  document.querySelectorAll('.aba').forEach((b) => {
    b.classList.toggle('ativa', b.dataset.aba === nome);
  });
  document.querySelectorAll('.aba-conteudo').forEach((c) => {
    c.classList.toggle('ativa', c.id === 'aba-' + nome);
  });
}

function escapa(t) {
  return String(t == null ? '' : t)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

document.querySelectorAll('.aba').forEach((b) => {
  b.onclick = () => trocaAba(b.dataset.aba);
});

$('btn-varrer').onclick = async () => {
  const pastas = $('pastas').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (!pastas.length) { alert('Informe pelo menos uma pasta.'); return; }
  limpaAvisos();
  const r = await api('/api/varrer', { pastas });
  if (r.erro) { mostraAvisos([r.erro], 'erro'); return; }
  iniciaPoll();
};

$('btn-cancelar').onclick = () => api('/api/cancelar', {});

$('btn-manifesto').onclick = async () => {
  const pastas = $('pastas').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (!pastas.length) { alert('Informe a pasta local (cartão/disco).'); return; }
  mostraAvisos(['gerando manifesto em ' + pastas[0] + ' ...']);
  const r = await api('/api/manifesto', { pasta: pastas[0] });
  limpaAvisos();
  if (r.erro) { mostraAvisos([r.erro], 'erro'); return; }
  const m = r.manifesto;
  mostraAvisos([
    `manifesto gerado: ${m.manifesto} — ${m.arquivos} arquivos, ` +
    `${fmtBytes(m.bytes_lidos)} lidos de ${fmtBytes(m.bytes_totais)}. ` +
    'Suba a pasta pro Drive com esse arquivo dentro.',
  ], 'ok');
};

$('btn-exportar').onclick = async () => {
  const dias = Array.from(document.querySelectorAll('.exp-dia:checked'))
    .map((e) => e.value);
  if (!dias.length) { alert('Selecione ao menos um dia.'); return; }
  $('exp-resultado').textContent = 'exportando...';
  const r = await api('/api/exportar', {
    saida: $('exp-saida').value.trim() || 'saida',
    dias,
    master: $('exp-master').checked,
    arquivo_unico: $('exp-unico').checked,
  });
  if (r.erro) { $('exp-resultado').textContent = 'ERRO: ' + r.erro; return; }
  const e = r.exportacao;
  $('exp-resultado').textContent =
    `${e.files.length} arquivo(s), ${e.clip_count} clipes posicionados\n` +
    e.files.join('\n') + '\n\nrelatório: ' + e.report_path +
    (e.warnings.length ? '\n\n! ' + e.warnings.join('\n! ') : '');
};

$('btn-refinar').onclick = async () => {
  if (!diaAtual) return;
  $('refino-resultado').textContent = 'analisando áudio...';
  const r = await api('/api/refinar-audio', { dia: diaAtual });
  if (r.erro) { $('refino-resultado').textContent = 'ERRO: ' + r.erro; return; }
  const lista = r.refinamentos || [];
  $('refino-resultado').textContent = lista.length
    ? lista.map((x) => `bloco ${x.bloco} take ${x.take} · ${x.arquivo}: ` +
                       `${x.ajuste_segundos >= 0 ? '+' : ''}${x.ajuste_segundos}s`).join('\n')
    : 'nenhum ajuste sugerido (takes com um único áudio ou sem transiente claro).';
};

$('btn-calibrar').onclick = async () => {
  const camera = $('cal-camera').value.trim();
  const iphone = $('cal-iphone').value.trim();
  if (!camera || !iphone) { alert('Informe os dois arquivos.'); return; }
  $('cal-resultado').textContent = 'lendo os dois clipes...';
  const r = await api('/api/calibrar', {
    arquivo_camera: camera, arquivo_iphone: iphone,
    refinar_audio: $('cal-audio').checked,
  });
  if (r.erro) { $('cal-resultado').textContent = 'ERRO: ' + r.erro; return; }
  const c = r.calibracao || {};
  $('cal-resultado').textContent = (c.message || '') + (c.note ? '\n' + c.note : '');
  estado = r;
  aplicaEstado();
};

$('btn-relatorio').onclick = async () => {
  const texto = await fetch('/api/relatorio').then((r) => r.text());
  $('modal-corpo').textContent = texto || 'nada organizado ainda.';
  $('modal').classList.remove('oculto');
};
$('modal-fechar').onclick = () => $('modal').classList.add('oculto');
$('modal').onclick = (e) => { if (e.target.id === 'modal') $('modal').classList.add('oculto'); };

$('limiar-dia').oninput = () => {
  $('limiar-dia-valor').textContent = fmtGap(Number($('limiar-dia').value));
};
$('limiar-dia').onchange = async () => {
  if (!diaAtual) return;
  estado = await api('/api/limiar',
                     { dia: diaAtual, segundos: Number($('limiar-dia').value) });
  aplicaEstado();
};
$('btn-limiar-dia-auto').onclick = async () => {
  if (!diaAtual) return;
  estado = await api('/api/limiar', { dia: diaAtual, segundos: null });
  aplicaEstado();
};

$('limiar-global').oninput = () => {
  $('limiar-global-valor').textContent = fmtGap(Number($('limiar-global').value));
};
$('btn-limiar-global').onclick = async () => {
  estado = await api('/api/limiar',
                     { todos: true, segundos: Number($('limiar-global').value) });
  aplicaEstado();
};
$('btn-limiar-auto').onclick = async () => {
  estado = await api('/api/limiar', { todos: true, segundos: null });
  aplicaEstado();
};

$('zoom').oninput = () => desenhaDia();

$('btn-limpar-cache').onclick = async () => {
  if (!confirm('Limpar o cache de metadados? A próxima varredura vai reler os arquivos.')) return;
  await api('/api/cache/limpar', {});
  await carregaEstado();
};

Object.keys(CAMPOS).forEach((id) => {
  const el = $(id);
  if (el) el.onchange = salvaConfig;
});
Object.keys(CHECKS).forEach((id) => {
  const el = $(id);
  if (el) el.onchange = salvaConfig;
});
$('exp-master').onchange = salvaConfig;

$('limiar-global-valor').textContent = fmtGap(Number($('limiar-global').value));
carregaEstado();
