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

> Esse token equivale às permissões da sua conta. O plugin guarda em `plugins/homeassistant/data/credential.json` (escrita atômica, permissões restritas) e exibe apenas os últimos 4 caracteres. **Nunca** cole em chat ou em captura de tela.

---

## 2. Conectar

1. Abra o N.E.K.O → painel de plugins → **Home Assistant · Casa inteligente**
2. Informe endereço e token
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

Também entende valores aproximados: `pela metade` = 50 %, `quase tudo` = 70 %, `o mais claro` / `o mais escuro` / `mínimo` / `máximo`, `um pouco mais`, `abaixa 10`, decimais como `26.5 graus`.

---

## 4. Dispositivos demais?

Com três luzes em casa, `acende a luz` dispara desambiguação:

```
Encontrados 3 dispositivos para «luz»:
1. 🟢 Sala Luminária de teto
2. 🔴 Quarto Luminária de teto
3. 🟢 Hall Downlight
Especifique com «área + dispositivo», por exemplo «quarto luminária de teto»
```

Duas saídas:

- **Adicione a área**: `acende a luminária de teto do quarto` (as áreas vêm das configurações de «Áreas» do HA, não as invente)
- **Defina um alias**: no painel, dê a `light.yeelight_ceiling_2` o nome que você realmente usa, por exemplo «luz principal»

---

## 5. Confirmação de segurança

Portões de garagem, portões e fechaduras **nunca são executados diretamente**. O plugin devolve um código de confirmação de 8 caracteres:

```
> abre o portão da garagem
< Vou executar «abre o portão da garagem». Há implicações de segurança. Código: a3f9c1d2
  Responda com esse código em 120 segundos para continuar.

> a3f9c1d2
< Feito: abre o portão da garagem
```

O código fica apenas na memória do processo, é **de uso único**, só o mais recente é mantido e expira em 120 s. Reiniciar o host o invalida.

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

**P: Aspirador robô / ventilador / fechadura?**
R: No momento o foco são seis domains básicos (light, switch, climate, cover, media_player, scene). Para os demais o plugin avisa claramente que não há suporte, em vez de fingir sucesso.

---
*Versão do documento: 2026-11-18*
