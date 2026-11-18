# Home Assistant · Casa inteligente · Início rápido

> **Em uma frase**: você já tem um Home Assistant rodando em casa; este plugin traduz frases como «acende a luz da sala» em chamadas aos services dele.

---

## 1. Preparação: endereço e token

### Descubra o endereço do Home Assistant

Abra a interface web do HA que você costuma usar e veja a barra de endereços:

- `http://homeassistant.local:8123`
- `http://192.168.1.50:8123`
- ou o domínio exposto pelo seu proxy reverso / túnel

Remova o **caminho da página** no final, deixando apenas `esquema://host:porta`. Por exemplo `http://192.168.1.50:8123/lovelace/0` → `http://192.168.1.50:8123` (o plugin também remove caminhos front-end comuns automaticamente, mas um valor limpo é mais seguro).

### Crie um token de acesso de longa duração

1. No HA clique no seu **avatar** (canto inferior esquerdo nas versões recentes)
2. Abra a aba **«Segurança»**
3. Role até **«Tokens de acesso de longa duração»**
4. Clique em **«Criar token»**, escolha qualquer nome (ex.: `N.E.K.O`)
5. **Copie imediatamente** — a string `eyJ0eXAiOiJKV1Qi...` aparece apenas uma vez

> Esse token equivale às permissões da sua conta. O plugin guarda **criptografado**: DPAPI no Windows (vinculado ao seu usuário e máquina), o chaveiro do sistema nas demais plataformas. Se nenhum estiver disponível, fica apenas na memória e precisa ser digitado de novo após reiniciar; **nunca** é gravado em texto puro. Apenas os últimos 4 caracteres são exibidos. **Nunca** cole em chat ou em captura de tela.

---

## 2. Conectar

1. Abra o N.E.K.O → painel de plugins → **Home Assistant · Casa inteligente**
2. Confirme o endereço — já vem preenchido com `http://127.0.0.1:8123`; troque pelo IP da máquina do HA se não for esta. Em seguida cole o token
3. Clique em **«Testar conexão»** — aparecerão a versão do HA, a quantidade de entidades e se o usuário é administrador
4. Clique em **«Salvar»**

Depois clique uma vez em **«Atualizar dispositivos»** para o plugin buscar a lista de entidades (em seguida há cache em memória por 60 segundos).

---

## 3. Controlar com a voz

Basta falar com seu companheiro de AI:

| Objetivo | Diga |
| --- | --- |
| Ligar/desligar | `acende a luz da sala` / `apaga a luz do escritório` |
| Brilho | `coloca a luz da sala em 40%` / `aumenta o brilho do abajur` |
| Temperatura de cor | `luz do quarto 3000K` / `deixa o teto mais quente` |
| Climatização | `coloca o ar em 26 graus` / `ar da sala no frio` / `velocidade do ar automática` |
| Persianas | `abre as cortinas` / `abre as cortinas pela metade` |
| Mídia | `volume da caixa 30` / `silencia a TV` / `pausa` |
| Cenas | `executa a cena chegar em casa` / `ativa o modo ausente` |

**A compreensão fica com o modelo de chat do N.E.K.O.** O plugin não analisa o texto. Por isso «metade» ou «um pouco mais» também funcionam: o modelo converte em `brightness: 50` ou pergunta antes um valor concreto. Quanto mais vago o pedido, mais provável que ele pergunte — o que é melhor do que agir no dispositivo errado.

---

## 4. Dispositivos demais?

Com três luzes em casa, `acende a luz` retorna «vários coincidem» e o assistente pergunta qual:

```
3 dispositivos «luz» coincidem:
1. Sala Luminária de teto (light.living_room_ceiling)
2. Quarto Luminária de teto (light.bedroom_ceiling)
3. Hall Spot embutido (light.hallway_downlight)
```

**O plugin nunca escolhe um por você** — prefere perguntar uma vez a errar. Três saídas:

- **Seja mais específico**: `acende a luminária do quarto` (nomes de área vêm das Áreas do HA, não são inventados)
- **Use um alias**: no painel, dê à entidade o nome que você realmente usa — por exemplo, renomeie `light.yeelight_ceiling_2` para «luz principal»
- **Diga a área**: `apaga todas as luzes do escritório` — o assistente lista antes as luzes daquela área e envia uma a uma após você confirmar

---

## 5. Confirmação de segurança

O plugin gerencia permissões em três níveis de risco, e **dispositivos de alto risco não são controláveis por padrão**:

| Nível | Dispositivos | Padrão |
| --- | --- | --- |
| Baixo | luzes, ventiladores, players, cenas, cortinas comuns | permitido |
| Médio | interruptores/tomadas, climatização | permitido (marcado no painel) |
| Alto | fechaduras, painéis de alarme, válvulas, aquecedores, portas de garagem | **bloqueado** |

Dispositivos de alto risco só são habilitados **um a um** no painel; não existe «selecionar todas as fechaduras». Mesmo assim, cada ação exige uma segunda confirmação: a autorização decide *se você pode* controlar, não *se ele pergunta antes*:

```
> abre a porta da frente
< Ação de alto risco, não executada. Vou executar «unlock» em «Fechadura da porta». Código: a3f9c1d2 (válido 120 s)

> a3f9c1d2
< Feito: lock.unlock — a releitura confirma «unlocked»
```

O código é de uso único, vinculado a «dispositivos + ação + parâmetros», morre se você trocar o alvo e expira em 120 s. Direções que tornam o mundo **mais seguro** — apagar, trancar, armar — nunca são barradas.

---

## 6. Ainda não tem Home Assistant? (modo experimento)

**Este plugin não inclui nem instala o Home Assistant.** É intencional: o HA é um serviço independente, a descoberta de dispositivos precisa acontecer na rede dele, e seu tamanho e ritmo de atualizações não cabem dentro de um plugin.

O caminho mais rápido para experimentar é Docker em uma máquina sempre ligada (NAS, mini PC ou até seu computador):

```bash
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  --network=host \
  -v /path/to/ha-config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```

Depois acesse `http://<IP-dessa-máquina>:8123`, crie a conta e emita um token conforme a seção 1. Não precisa de dispositivos reais: o HA permite adicionar entidades virtuais (`input_boolean`, luzes de template), o que já valida o plugin.

> ⚠️ Use `--network=host` para o HA descobrir dispositivos da LAN (especialmente o que depende de broadcast, como Mijia/Tuya). Com rede bridge eles se perdem.

---

## 7. Perguntas frequentes

**P: Diz «não configurado».**
R: A conexão ainda não foi salva. Informe endereço e token e clique em Salvar.

**P: O teste falha com 401 / 403.**
R: Token incorreto ou revogado. Crie outro na página «Segurança» do HA. Um 403 geralmente indica permissões insuficientes do usuário.

**P: O teste não alcança o host.**
R: O plugin roda na máquina do N.E.K.O e precisa alcançar o HA **a partir dela**. Primeiro teste `ping` ou abra a URL no navegador dessa máquina. Se `homeassistant.local` não resolver, use o IP.

**P: Controla os dispositivos, mas nunca reconhece nomes de área.**
R: Os registries WebSocket não estão disponíveis (`get_status` mostra o motivo). O mais comum é o usuário do token não ser administrador — crie o token com uma conta admin. Em modo degradado o pareamento ainda funciona pelo nome da entidade.

**P: Dispositivos novos não aparecem.**
R: Clique em «Atualizar dispositivos» no painel. O snapshot de entidades tem cache de 60 segundos.

**Q: Suporta aspirador robô / umidificador / fechadura?**
A: Fechaduras sim, mas são de alto risco: habilite uma a uma no painel e cada ação passa por uma segunda confirmação. Ventiladores, válvulas, aquecedores, painéis de alarme e scripts também são suportados. Aspiradores robô, umidificadores e câmeras ainda não são controláveis — o plugin diz isso claramente em vez de fingir. Sensores não são controláveis, mas **podem ser consultados**.

---
*Versão do documento: 2026-11-18*
