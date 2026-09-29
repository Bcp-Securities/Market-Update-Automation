// =====================================================================
// Utilitários gerais
// =====================================================================

let atualizarListaAtivosGlobal = null;

const LABELS = {
    bbg_id: 'ID',
    isin: 'ISIN',
    ticker: 'Ticker',
    coupon: 'Coupon',
    maturity: 'Maturity',
    issue_date: 'Issue Date',
    industry_group: 'Industry Group',
    cntry_of_risk: 'Country of Risk',
    issuer: 'Issuer',
    currency: 'Currency',
    collateral: 'Collateral',
    amt_issuance: 'Amount Issuance',
    min_piece: 'Min. Piece',
    bbg_id_regs: 'BBG ID RegS',
    bbg_id_144a: 'BBG ID 144A',
};

function showToast(mensagem, tipo = 'success') {
    const stack = document.getElementById('toast-stack');
    const toast = document.createElement('div');
    toast.className = `toast is-${tipo}`;
    toast.textContent = mensagem;
    stack.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function setFieldHint(el, mensagem, tipo) {
    el.textContent = mensagem || '';
    el.classList.remove('is-error', 'is-success');
    if (tipo) el.classList.add(`is-${tipo}`);
}

function setButtonLoading(btn, loading) {
    const label = btn.querySelector('.btn-label');
    const spinner = btn.querySelector('.btn-spinner');
    btn.disabled = loading;
    if (spinner) spinner.hidden = !loading;
    if (label) label.style.opacity = loading ? 0.6 : 1;
}

async function apiRequest(url, options = {}) {
    const response = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.mensagem || 'Erro inesperado no servidor.');
    }
    return data;
}

// =====================================================================
// Navegação entre abas
// =====================================================================

function initTabs() {
    const buttons = document.querySelectorAll('.tab-btn');
    buttons.forEach((btn) => {
        btn.addEventListener('click', () => {
            buttons.forEach((b) => b.classList.remove('is-active'));
            document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('is-active'));

            btn.classList.add('is-active');
            document.getElementById(`panel-${btn.dataset.tab}`).classList.add('is-active');
        });
    });
}

// =====================================================================
// Aba 1 — Cadastrar novo ativo
// =====================================================================

function initAbaCadastrar() {
    const form = document.getElementById('form-consultar');
    const input = document.getElementById('input-bloomberg-id');
    const btnConsultar = document.getElementById('btn-consultar');
    const feedback = document.getElementById('consultar-feedback');
    const resultado = document.getElementById('resultado-consulta');
    const grid = document.getElementById('grid-consulta');
    const btnConfirmar = document.getElementById('btn-confirmar-cadastro');
    const btnCancelar = document.getElementById('btn-cancelar-cadastro');
    const extraCampo1 = document.getElementById('extra-campo1');
    const extraCampo2 = document.getElementById('extra-campo2');

    let dadosAtuais = null;

    function renderGrid(dados) {
        grid.innerHTML = '';
        Object.entries(dados).forEach(([chave, valor]) => {
            const dt = document.createElement('dt');
            dt.textContent = LABELS[chave] || chave;
            const dd = document.createElement('dd');
            dd.textContent = valor;
            grid.appendChild(dt);
            grid.appendChild(dd);
        });
    }

    function resetResultado() {
        resultado.hidden = true;
        dadosAtuais = null;
        extraCampo1.value = '';
        extraCampo2.value = '';
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const bloombergId = input.value.trim();
        if (!bloombergId) return;

        setFieldHint(feedback, '');
        setButtonLoading(btnConsultar, true);
        resetResultado();

        try {
            const data = await apiRequest('/api/consultar-bloomberg', {
                method: 'POST',
                body: JSON.stringify({ bloomberg_id: bloombergId }),
            });
            dadosAtuais = data.dados;
            renderGrid(dadosAtuais);
            resultado.hidden = false;
        } catch (err) {
            setFieldHint(feedback, err.message, 'error');
        } finally {
            setButtonLoading(btnConsultar, false);
        }
    });

    btnCancelar.addEventListener('click', () => {
        resetResultado();
        input.value = '';
        input.focus();
    });

    btnConfirmar.addEventListener('click', async () => {
        if (!dadosAtuais) return;
        btnConfirmar.disabled = true;

        const payload = {
            ...dadosAtuais,
            bbg_id_regs: extraCampo1.value.trim() || null,
            bbg_id_144a: extraCampo2.value.trim() || null,
        };

        try {
            const data = await apiRequest('/api/cadastrar-ativo', {
                method: 'POST',
                body: JSON.stringify(payload),
            });
            showToast(data.mensagem, data.sucesso ? 'success' : 'error');
            resetResultado();
            form.reset();
        } catch (err) {
            showToast(err.message, 'error');
        } finally {
            btnConfirmar.disabled = false;
        }
    });
}

// =====================================================================
// Aba 2 — Editar ativo existente (formulário construído dinamicamente
// a partir do "schema" que o back-end envia junto com /api/ativo/<id>)
// =====================================================================
 
// Cria o elemento de campo (label + input) de acordo com o tipo declarado
// no schema. Para adicionar suporte a um novo tipo de coluna, basta
// acrescentar um "case" aqui — nada mais no resto do arquivo muda.
function criarCampoEdicao(coluna, valorAtual) {
    const wrapper = document.createElement('div');
    wrapper.className = 'field-group';
    if (coluna.tipo === 'boolean') wrapper.classList.add('field-group-inline');
 
    const inputId = `edit-${coluna.nome}`;
 
    const label = document.createElement('label');
    label.setAttribute('for', inputId);
    label.textContent = coluna.label || coluna.nome;
 
    let campo;
 
    switch (coluna.tipo) {
        case 'select': {
            campo = document.createElement('select');
            (coluna.opcoes || []).forEach((opcao) => {
                const optEl = document.createElement('option');
                optEl.value = opcao;
                optEl.textContent = opcao;
                campo.appendChild(optEl);
            });
            campo.value = valorAtual ?? '';
            break;
        }
 
        case 'textarea': {
            campo = document.createElement('textarea');
            campo.rows = 3;
            campo.value = valorAtual ?? '';
            break;
        }
 
        case 'boolean': {
            campo = document.createElement('input');
            campo.type = 'checkbox';
            campo.checked = Boolean(valorAtual);
            break;
        }
 
        case 'number': {
            campo = document.createElement('input');
            campo.type = 'number';
            campo.value = valorAtual ?? '';
            break;
        }
 
        case 'date': {
            campo = document.createElement('input');
            campo.type = 'date';
            campo.value = valorAtual ?? '';
            break;
        }
 
        case 'text':
        default: {
            campo = document.createElement('input');
            campo.type = 'text';
            campo.value = valorAtual ?? '';
            break;
        }
    }
 
    campo.id = inputId;
    campo.name = coluna.nome;
    campo.dataset.tipo = coluna.tipo || 'text';
    if (coluna.somente_leitura) {
        campo.disabled = true;
    }
 
    if (coluna.tipo === 'boolean') {
        // Checkbox fica ao lado do label, não em bloco.
        wrapper.appendChild(campo);
        wrapper.appendChild(label);
    } else {
        wrapper.appendChild(label);
        wrapper.appendChild(campo);
    }
 
    return wrapper;
}
 
// Lê o valor de volta de um campo já renderizado, respeitando seu tipo.
function lerValorCampo(campo) {
    if (campo.dataset.tipo === 'boolean') return campo.checked;
    if (campo.dataset.tipo === 'number') return campo.value === '' ? null : Number(campo.value);
    return campo.value;
}

function initAbaEditar(listaAtivos) {
    const selectEl = document.getElementById('select-editar');
    const card = document.getElementById('editar-card');
    const idBadge = document.getElementById('editar-id-badge');
    const feedback = document.getElementById('editar-feedback');
    const btnSalvar = document.getElementById('btn-salvar-edicao');
    const btnCancelar = document.getElementById('btn-cancelar-edicao');
    const formEl = document.getElementById('form-editar');
 
    const choices = new Choices(selectEl, {
        searchEnabled: true,
        itemSelectText: '',
        placeholder: true,
        placeholderValue: 'Buscar ativo por nome ou ID...',
        choices: listaAtivos.map((a) => ({ value: a.bbg_id, label: a.label})),
    });
 
    let ativoAtual = null;
    let schemaAtual = [];
 
    function renderForm(schema, dados) {
        formEl.innerHTML = '';
        schema.forEach((coluna) => {
            formEl.appendChild(criarCampoEdicao(coluna, dados[coluna.nome]));
        });
    }
 
    function coletarDadosForm() {
        const resultado = { id: ativoAtual.id };
        schemaAtual.forEach((coluna) => {
            const campo = document.getElementById(`edit-${coluna.nome}`);
            resultado[coluna.nome] = lerValorCampo(campo);
        });
        return resultado;
    }
 
    selectEl.addEventListener('change', async () => {
        const ativoId = selectEl.value;
        if (!ativoId) return;
 
        setFieldHint(feedback, '');
        card.hidden = true;
 
        try {
            const data = await apiRequest(`/api/ativo/${encodeURIComponent(ativoId)}`);
            ativoAtual = data.dados;
            schemaAtual = data.schema || [];
            idBadge.textContent = ativoAtual.bbg_id;
            renderForm(schemaAtual, ativoAtual);
            card.hidden = false;
        } catch (err) {
            showToast(err.message, 'error');
        }
    });
 
    btnCancelar.addEventListener('click', () => {
        if (ativoAtual) renderForm(schemaAtual, ativoAtual);
        setFieldHint(feedback, '');
    });
 
    btnSalvar.addEventListener('click', async () => {
        if (!ativoAtual) return;
 
        const dadosEditados = coletarDadosForm();
 
        btnSalvar.disabled = true;
        try {
            const data = await apiRequest('/api/atualizar-ativo', {
                method: 'POST',
                body: JSON.stringify(dadosEditados),
            });
            ativoAtual = dadosEditados;
            setFieldHint(feedback, data.mensagem, 'success');
            showToast(data.mensagem, 'success');
        } catch (err) {
            setFieldHint(feedback, err.message, 'error');
        } finally {
            btnSalvar.disabled = false;
        }
    });

    return function atualizarListaAtivos(novaLista) {
        choices.clearChoices();
        choices.setChoices(
            novaLista.map((a) => ({
                value: a.bbg_id,
                label: a.label
            })),
            'value',
            'label',
            true
        );
    };
}

// =====================================================================
// Aba 3 — Preenchimento em lote (com sub-abas para diferentes tipos)
// =====================================================================

// Cada entrada representa um tipo de preenchimento em lote e liga os
// elementos do DOM daquela sub-aba pelo sufixo de id / data-lote-tipo.
// Para adicionar um novo tipo: (1) copie um bloco de sub-aba no HTML
// trocando "tipoN", (2) adicione o botão em .subtab-bar, (3) adicione
// uma entrada aqui.
const TIPOS_LOTE = ['todos', 'tipo1', 'tipo2', 'tipo3', 'bdp'];

// Tipos cujo preenchimento depende dos IDs RegS/144A e por isso passam
// pela verificação/painel de correção antes de rodar.
const TIPOS_DEPENDENTES_DE_HOLDERS = ['tipo2', 'tipo3'];

// Tipos que precisam de período. Holders e BDP sempre trazem o dado mais
// atual e ignoram datas. (Manter em sincronia com TIPOS_QUE_USAM_DATA no app.py)
const TIPOS_COM_DATA = ['tipo1', 'tipo3'];

function initSubTabsLote() {
    const buttons = document.querySelectorAll('.subtab-btn');
    buttons.forEach((btn) => {
        btn.addEventListener('click', () => {
            buttons.forEach((b) => b.classList.remove('is-active'));
            document.querySelectorAll('.subtab-panel').forEach((p) => p.classList.remove('is-active'));

            btn.classList.add('is-active');
            document.getElementById(`subpanel-${btn.dataset.subtab}`).classList.add('is-active');
        });
    });
}

// ---------------------------------------------------------------------
// Helpers do painel de verificação de IDs (Holders)
// ---------------------------------------------------------------------

function criarBotao(texto, classes, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `btn ${classes}`;
    btn.textContent = texto;
    btn.addEventListener('click', onClick);
    return btn;
}

// Uma linha por ativo: mostra o ID geral e os dois IDs de Holders.
// O que já está preenchido aparece travado; o que falta vira input editável.
function criarLinhaHolders(ativo) {
    const linha = document.createElement('div');
    linha.className = 'holders-row';
    linha.dataset.bbgId = ativo.bbg_id;

    const titulo = document.createElement('div');
    titulo.className = 'holders-row-title';
    titulo.textContent = ativo.bbg_id;
    linha.appendChild(titulo);

    const campos = document.createElement('div');
    campos.className = 'holders-row-fields';

    ['bbg_id_regs', 'bbg_id_144a'].forEach((chave) => {
        const grupo = document.createElement('div');
        grupo.className = 'field-group';

        const label = document.createElement('label');
        label.textContent = LABELS[chave];

        const input = document.createElement('input');
        input.type = 'text';
        input.dataset.campo = chave;
        if (ativo[chave]) {
            input.value = ativo[chave];
            input.disabled = true;
        } else {
            input.placeholder = 'Não preenchido';
        }

        grupo.append(label, input);
        campos.appendChild(grupo);
    });

    linha.appendChild(campos);
    return linha;
}

// Lê só os inputs editáveis que o usuário preencheu.
function coletarCorrecoesHolders(listaEl) {
    const correcoes = [];
    listaEl.querySelectorAll('.holders-row').forEach((linha) => {
        const item = { bbg_id: linha.dataset.bbgId };
        let alterou = false;
        linha.querySelectorAll('input:not(:disabled)').forEach((input) => {
            const valor = input.value.trim();
            if (valor) {
                item[input.dataset.campo] = valor;
                alterou = true;
            }
        });
        if (alterou) correcoes.push(item);
    });
    return correcoes;
}

// =====================================================================
// Aba 3: função principal
// =====================================================================

function initAbaLote(listaAtivos) {
    initSubTabsLote();

    const choicesPorTipo = {};

    TIPOS_LOTE.forEach((tipo) => {
        const selectEl = document.getElementById(`select-lote-${tipo}`);
        // Podem ser null (Holders e BDP não têm campos de data)
        const dataInicialEl = document.getElementById(`data-inicial-${tipo}`);
        const dataFinalEl = document.getElementById(`data-final-${tipo}`);
        const dateRangeEl = document.querySelector(`#subpanel-${tipo} .date-range`);
        const btnProcessar = document.querySelector(`.btn-processar-lote[data-lote-tipo="${tipo}"]`);
        const feedback = document.querySelector(`.lote-feedback[data-lote-tipo="${tipo}"]`);
        const resultado = document.querySelector(`.lote-resultado[data-lote-tipo="${tipo}"]`);
        const lista = document.querySelector(`.lote-lista[data-lote-tipo="${tipo}"]`);

        const choices = new Choices(selectEl, {
            searchEnabled: true,
            removeItemButton: true,
            placeholder: true,
            placeholderValue: 'Selecione um ou mais ativos...',
            choices: listaAtivos.map((a) => ({
                value: a.bbg_id,
                label: a.label,
            })),
        });

        choicesPorTipo[tipo] = choices;

        // Painel de verificação de IDs: existe em qualquer sub-aba onde um
        // tipo dependente de RegS/144A pode rodar.
        let painelHolders = null;
        if (TIPOS_DEPENDENTES_DE_HOLDERS.includes(tipo) || tipo === 'todos') {
            painelHolders = document.createElement('div');
            painelHolders.className = 'card result-card holders-check';
            painelHolders.hidden = true;
            resultado.before(painelHolders);
        }

        // ---------------- Quais tipos esta sub-aba vai rodar ----------------

        function obterTiposEfetivos() {
            if (tipo !== 'todos') return [tipo];
            return Array.from(document.querySelectorAll('#todos-tipos input:checked'))
                .map((el) => el.value);
        }

        function precisaData(tipos) {
            return tipos.some((t) => TIPOS_COM_DATA.includes(t));
        }

        function atualizarEstadoDatas() {
            if (!dateRangeEl) return;
            const precisa = precisaData(obterTiposEfetivos());
            dateRangeEl.classList.toggle('is-disabled', !precisa);
            dateRangeEl.toggleAttribute('inert', !precisa);
        }

        if (tipo === 'todos') {
            document.getElementById('todos-tipos').addEventListener('change', atualizarEstadoDatas);
            atualizarEstadoDatas();
        }

        // ---------------- Execução ----------------

        async function comCarregamento(fn) {
            setButtonLoading(btnProcessar, true);
            try {
                await fn();
            } catch (err) {
                setFieldHint(feedback, err.message, 'error');
            } finally {
                setButtonLoading(btnProcessar, false);
            }
        }

        async function executar(rodada, holdersConfirmado) {
            resultado.hidden = true;
            setFieldHint(feedback, '');

            const data = await apiRequest('/api/preencher-lote', {
                method: 'POST',
                body: JSON.stringify({
                    tipo_preenchimento: tipo,
                    subtipos: tipo === 'todos' ? rodada.tipos : undefined,
                    ativo_ids: rodada.selecionados,
                    data_inicial: rodada.dataInicial,
                    data_final: rodada.dataFinal,
                    holders_confirmado: holdersConfirmado,
                }),
            });

            lista.innerHTML = '';
            data.detalhes.forEach((item) => {
                const li = document.createElement('li');
                li.innerHTML = `
                    <span class="result-id">${item.id}</span>
                    <div>
                        <span>${item.mensagem}</span>
                        <span class="result-status ${item.status}">
                            ${item.status === 'ok' ? 'OK' : 'ERRO'}
                        </span>
                    </div>
                `;
                lista.appendChild(li);
            });

            resultado.hidden = false;
            showToast(data.mensagem, data.sucesso ? 'success' : 'error');
        }

        // Decide se roda direto ou se precisa passar pela verificação de Holders
        async function avancar(rodada) {
            const dependeDeHolders = rodada.tipos.some((t) => TIPOS_DEPENDENTES_DE_HOLDERS.includes(t));
            if (!dependeDeHolders) {
                return executar(rodada, false);
            }

            const avaliacao = await apiRequest('/api/holders/verificar', {
                method: 'POST',
                body: JSON.stringify({ ativo_ids: rodada.selecionados }),
            });

            if (avaliacao.status === 'ok') {
                painelHolders.hidden = true;
                return executar(rodada, false);
            }

            mostrarPainelHolders(rodada, avaliacao);
        }

        function mostrarPainelHolders(rodada, avaliacao) {
            const bloqueado = avaliacao.status === 'bloqueado';
            const problemas = avaliacao.ativos.filter((a) => a.cenario !== 3);
            const qtdOk = avaliacao.ativos.length - problemas.length;

            painelHolders.innerHTML = '';

            const header = document.createElement('div');
            header.className = 'result-header';
            const titulo = document.createElement('h3');
            titulo.textContent = 'Séries necessárias';
            const badge = document.createElement('span');
            badge.className = `badge ${bloqueado ? 'badge-danger' : 'badge-warning'}`;
            badge.textContent = bloqueado ? 'Bloqueado' : 'Atenção';
            header.append(titulo, badge);

            const msg = document.createElement('p');
            msg.className = 'holders-msg';
            msg.textContent = bloqueado
                ? 'Não é possível buscar os dados: há ativo(s) sem nenhum dos dois IDs (RegS e 144A). Preencha ao menos um deles abaixo para continuar.'
                : 'Há ativo(s) com apenas um dos dois IDs preenchido. Você pode completar agora ou prosseguir usando apenas o ID existente.';

            const listaProblemas = document.createElement('div');
            listaProblemas.className = 'holders-list';
            problemas.forEach((a) => listaProblemas.appendChild(criarLinhaHolders(a)));

            const hint = document.createElement('p');
            hint.className = 'field-hint';

            const acoes = document.createElement('div');
            acoes.className = 'action-row';

            acoes.appendChild(criarBotao('Cancelar', 'btn-ghost', () => {
                painelHolders.hidden = true;
            }));

            if (!bloqueado) {
                acoes.appendChild(criarBotao('Prosseguir mesmo assim', 'btn-ghost', () => {
                    painelHolders.hidden = true;
                    comCarregamento(() => executar(rodada, true));
                }));
            }

            const btnSalvar = criarBotao('Salvar IDs e continuar', 'btn-primary', async () => {
                const correcoes = coletarCorrecoesHolders(listaProblemas);
                if (!correcoes.length) {
                    setFieldHint(hint, 'Preencha ao menos um campo para salvar.', 'error');
                    return;
                }

                btnSalvar.disabled = true;
                try {
                    const salvo = await apiRequest('/api/holders/salvar-ids', {
                        method: 'POST',
                        body: JSON.stringify({ correcoes }),
                    });
                    if (!salvo.sucesso) {
                        setFieldHint(hint, salvo.mensagem, 'error');
                        return;
                    }
                    showToast(salvo.mensagem, 'success');
                    // Reavalia: se agora estiver tudo certo, segue sozinho;
                    // se ainda faltar algo, o painel reaparece atualizado.
                    await comCarregamento(() => avancar(rodada));
                } catch (err) {
                    setFieldHint(hint, err.message, 'error');
                } finally {
                    btnSalvar.disabled = false;
                }
            });
            acoes.appendChild(btnSalvar);

            painelHolders.append(header, msg, listaProblemas);
            if (qtdOk > 0) {
                const nota = document.createElement('p');
                nota.className = 'holders-note';
                nota.textContent = `${qtdOk} ativo(s) já com os dois IDs preenchidos (não listados).`;
                painelHolders.appendChild(nota);
            }
            painelHolders.append(hint, acoes);
            painelHolders.hidden = false;
        }

        // ---------------- Clique em "Processar" ----------------

        btnProcessar.addEventListener('click', () => {
            const tipos = obterTiposEfetivos();
            const selecionados = choices.getValue(true);
            const usaData = precisaData(tipos);
            const dataInicial = usaData ? dataInicialEl.value : null;
            const dataFinal = usaData ? dataFinalEl.value : null;

            if (painelHolders) painelHolders.hidden = true;

            if (!tipos.length) {
                setFieldHint(feedback, 'Selecione ao menos um tipo de preenchimento.', 'error');
                return;
            }
            if (!selecionados.length) {
                setFieldHint(feedback, 'Selecione ao menos um ativo.', 'error');
                return;
            }
            if (usaData) {
                if (!dataInicial) {
                    setFieldHint(feedback, 'Informe a data inicial.', 'error');
                    dataInicialEl.focus();
                    return;
                }
                if (!dataFinal) {
                    setFieldHint(feedback, 'Informe a data final.', 'error');
                    dataFinalEl.focus();
                    return;
                }
                if (dataInicial > dataFinal) {
                    setFieldHint(feedback, 'A data inicial não pode ser posterior à data final.', 'error');
                    dataInicialEl.focus();
                    return;
                }
            }

            setFieldHint(feedback, '');
            comCarregamento(() => avancar({ tipos, selecionados, dataInicial, dataFinal }));
        });
    });

    return function atualizarListaAtivos(novaLista) {
        Object.values(choicesPorTipo).forEach((choices) => {
            choices.clearChoices();
            choices.setChoices(
                novaLista.map((a) => ({
                    value: a.bbg_id,
                    label: a.label,
                })),
                'value',
                'label',
                true
            );
        });
    };
}

// =====================================================================
// Bootstrap
// =====================================================================

document.addEventListener('DOMContentLoaded', async () => {
    initTabs();
    initAbaCadastrar();
    
    try {
        const data = await apiRequest('/api/ativos');
        const atualizarListaEditar = initAbaEditar(data.ativos);
        const atualizarListaLote = initAbaLote(data.ativos);
        
        atualizarListaAtivosGlobal = async () => {
            const novaData = await apiRequest('/api/ativos');

            atualizarListaEditar(novaData.ativos);
            atualizarListaLote(novaData.ativos);
        };
    } catch (err) {
        showToast('Não foi possível carregar a lista de ativos.', 'error');
    }
});