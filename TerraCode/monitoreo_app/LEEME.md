# Monitoreo acústico ODS 15 – Parque Chicaque

Aplicación de escritorio (ventana) que analiza audio de sensores, genera alertas en tiempo real y las envía por SMS.

## 1. Iniciar (Windows)
1. Instala Python 3.10 o superior desde python.org marcando **Add python.exe to PATH** (incluye Tkinter).
2. Doble clic en **INICIAR.bat**. La primera vez instala las librerías; luego se abre la ventana.
   (Equivalente manual: `pip install -r requirements.txt` y `python monitoreo.py`.)

Para MP3, M4A y otros formatos que no sean WAV hace falta tener `ffmpeg` instalado.

## 2. Demostración rápida
- **Generar 4 Señales de Prueba**: analiza disparo, motosierra, motor y bosque. Debe dar 4/4 de precisión.
  Son señales sintéticas: para medir con sonidos reales usa `herramientas/evaluar_con_esc50.py` (sección *Medir la precisión*).
- **Agregar Audios / Cargar Carpeta**: analiza tus propios audios. Los KPIs de precisión y falsas alarmas se calculan por el
  nombre del archivo (por ejemplo `motosierra_1.wav`, `disparo_2.wav`, `incendio_3.wav`, `bosque_4.wav`).

## 3. Sensores en tiempo real
1. Botón **📡 Sensores y SMS** → pestaña *Sensores*.
2. Registra el sensor (ID, nombre, latitud, longitud). El **token** queda copiado al portapapeles.
3. Pulsa **Iniciar servidor**. Si el sensor está en otro equipo/dispositivo de la misma red, marca *Aceptar sensores de otros equipos*
   y permite el puerto en el firewall de Windows (aparece un aviso la primera vez).
4. El sensor envía el audio con `POST http://IP:PUERTO/audio` y las cabeceras `X-Sensor-Id` y `X-Sensor-Token`
   (cuerpo = archivo de audio, máx. 15 MB). Para probarlo sin hardware:

       python sensor_cliente.py --url http://localhost:8000 --id S01 --token TU_TOKEN --archivo bosque.wav --trozo 5
       python sensor_cliente.py --url http://IP:8000 --id S01 --token TU_TOKEN --microfono     (pip install sounddevice)

   Un microcontrolador (ESP32, Raspberry Pi) solo debe hacer ese mismo POST. Se recomiendan trozos de 3 a 10 s.
5. Sin ventana (por ejemplo en un servidor): `python monitoreo.py servidor --red` y `python monitoreo.py sensor S01 --lat 4.601 --lon -74.315`.

## Usar un CELULAR como sensor real (sin instalar apps)
El celular abre una página del programa, graba con su micrófono y envía un trozo de audio cada 5 s, igual que un sensor
del bosque. En la pantalla del celular se ve lo que detectó el sistema (🚨 MOTOSIERRA 97 %, vibra si es amenaza) y,
si se permite el GPS, la alerta lleva la ubicación real del celular.

1. **📡 Sensores y SMS → 📱 Conectar celular**. Elige un ID (p. ej. `CEL-01`). El programa registra el sensor, inicia el
   servidor en red y muestra una dirección (`https://IP_DEL_PC:8443/celular`) y un **código de 6 dígitos**.
2. En el celular (Chrome o Safari) abre esa dirección. Aparece «La conexión no es privada»: es normal (el certificado lo
   crea el programa). Toca *Configuración avanzada → Continuar*.
3. Escribe el código, pulsa **Empezar a escuchar** y permite el micrófono y la ubicación.

Requisitos: celular y computador en la **misma red Wi-Fi** (en redes de universidades suele estar bloqueado: usa el
punto de acceso del celular y conecta el PC a él), permitir a Python en el firewall de Windows (redes privadas), y
dejar la pantalla del celular encendida con la página abierta (el programa pide que no se apague).
El celular graba sin los filtros de voz del teléfono, para no borrar el ruido de una motosierra.
Por consola: `python monitoreo.py servidor --red` y en otra ventana `python monitoreo.py celular CEL-01`.

**Probar con disparos reproducidos por un parlante:** el celular envía trozos de 5 s con 1 s de traslape (un disparo en
el corte llega completo), el programa sube el volumen de lo que recibe (los audios guardados se oyen normal) y la
página avisa si el nivel es muy bajo. Si aun así algunos no se detectan, **marca la categoría real** (clic derecho en
la tabla → *Categoría real: disparo*; también *▶ Escuchar audio*) y entrena con **🎓 Entrenar con mis audios**
eligiendo la carpeta `entrenamiento/`: así el sistema aprende la cadena real parlante → habitación → celular.
Las marcas también alimentan el KPI de precisión.

**Si el celular dice que no encuentra la página:**
1. Escribe la dirección completa en la barra de direcciones (con `https://` y `:8443`), no en el buscador.
2. Prueba las otras direcciones que muestra el programa (el PC puede tener varias: Wi-Fi, VPN, VirtualBox...).
3. Pulsa **🛡 Permitir** en la ventana (o `python monitoreo.py firewall`): crea la regla del firewall de Windows.
4. Desde el celular abre `http://IP:8000/` : si tampoco carga, el problema es la red (no el programa): usa el punto de
   acceso del celular y conecta el PC a él.

## 4. Alertas por SMS
Pestaña *Alertas por SMS*:
- Agrega los celulares de los guardabosques (`3001234567` o `+573001234567`).
- Proveedor **simulado** (por defecto): no envía nada, guarda el mensaje en el historial; sirve para la demostración.
- Proveedor **twilio**: crea una cuenta en twilio.com, copia Account SID, Auth Token y un número emisor. La cuenta de prueba solo envía a números verificados.
- Proveedor **webhook**: envía un POST JSON `{"to": "...", "message": "..."}` a tu propia URL (por ejemplo una app de pasarela SMS en un teléfono Android).
- Para no saturar, se envía un SMS por sensor cada N segundos (por defecto 60) y se puede exigir nivel *alta*.
- Pulsa **Enviar SMS de prueba** antes de confiar en el sistema.

Los datos de Twilio se guardan en `configuracion.json` (junto al programa). No lo compartas. También se pueden definir las variables de entorno
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` y `TWILIO_FROM`.

## 5. Cómo funciona la alerta
1. **Etapa acústica (menos de 1 s)**: dos detectores analizan el audio y se combinan:
   - **Red neuronal YAMNet** (`clasificador_sonidos.py` + `modelos/yamnet.npz`): una red de Google entrenada con millones
     de sonidos reales (AudioSet, 521 clases: *Chainsaw*, *Gunshot*, *Fire*, *Engine*, *Rain*, *Bird*...). **Escucha** el audio.
     Corre con NumPy puro: no hace falta instalar TensorFlow, ni GPU, ni internet. Las grabaciones débiles (sensor lejos) se
     amplifican hasta +30 dB antes de pasarlas a la red.
   - **Reglas acústicas**: energía, periodicidad, bandas de frecuencia y conteo de golpes. Muy rápidas y buenas con impulsos.
   - La **fusión** usa lo mejor de cada uno: la red descarta falsas motosierras (insectos, gallos, sirenas, campanas,
     aeronaves) y falsos disparos (trueno, goteo); las reglas aportan el detalle del impulso del disparo.
   Si detecta **motosierra, disparo o incendio**, guarda la alerta y envía el SMS en ese momento.
2. **Etapa de IA (LM Studio)**: se ejecuta después, como *auditora*. Recibe lo que oyó la red, lo que dicen las reglas, 8
   métricas con nombre claro y el **contexto** (hora local, detecciones recientes del mismo sensor y alertas de sensores
   vecinos en 2 km). Si contradice una alerta débil, esta queda **REVISAR (posible falsa alarma)** y se envía un SMS de aviso.
   La IA de texto nunca crea una amenaza sin evidencia acústica ni anula una detección concluyente.
   Por rapidez, solo se consulta cuando la etapa acústica no es concluyente; marca **IA en todas** para consultarla siempre.

## Enviar solo cuando haya sonido (sensores y celulares)
En **🔒 Administración → Sensores: enviar solo cuando haya sonido** (o `python monitoreo.py admin sonido-on media`):
- El celular o sensor sigue escuchando, pero **no envía los trozos en silencio**: aprende el nivel del ruido de fondo y
  envía en cuanto algo sobresale (un golpe como un disparo, o un motor que arranca), más el trozo siguiente.
- **Sensibilidad:** alta (sonidos suaves o lejanos, +6 dB), media (recomendado, +10 dB), baja (solo fuertes, +15 dB).
- Aunque haya silencio, cada sensor envía **un trozo cada 2 minutos** para confirmar que sigue funcionando.
- Los celulares toman el cambio en unos 20 s y los sensores de PC en unos 30 s, sin reconectarlos. La pantalla del
  celular muestra el modo y cuántos trozos en silencio se ahorraron; la consola del programa también lo indica.
- Ventaja: menos datos por la red, menos análisis y menos consultas a la IA. Para pruebas o demostraciones con sonido
  continuo se puede dejar apagado (envío continuo).

## Cola de IA (todos los audios se revisan, uno por uno)
- Cada audio pasa primero por la etapa acústica (< 1 s): si hay amenaza, **la alerta y el aviso a Telegram/SMS salen
  de inmediato**, sin esperar a la IA.
- Después entra en la **cola de IA** (guardada en la base de datos). Un solo trabajador envía los audios a LM Studio
  **de uno en uno**, en orden. La cabecera muestra «🧠 IA: N en cola» y la tabla dice «⏳ en cola» o el tiempo de la IA.
- Si LM Studio está apagado o falla, el audio **no se descarta**: espera y se reintenta (hasta 4 veces si responde mal;
  sin límite mientras esté apagado). Si se cierra el programa, la cola se retoma al abrirlo.
- Con **IA en todas** (🔒 Administración, activado por defecto) la IA revisa todos los audios; sin ella, solo los dudosos.
- Reglas para que la IA no cause errores: solo elige entre categorías que la evidencia acústica hace posibles; no
  puede cambiar un resultado acústico concluyente; **nunca convierte una alerta en "no amenaza"** (como mucho deja la
  nota «La IA sugiere revisar»); y si ve una amenaza que la red respalda (≥ 30 %), envía la alerta.

## Barra de botones
Si la pantalla es pequeña, la barra de botones se desplaza: usa la barrita de abajo o la rueda del mouse sobre ella
para llegar a 🔒 Administración, 📡 Sensores y SMS, 🔌 Probar IA y 🗑 Limpiar Todo.

## 6. LM Studio
- Instala un modelo **Instruct** (por ejemplo *Llama 3.1 8B Instruct* o *Qwen 2.5 7B Instruct*). Un modelo *base* (sin
  'Instruct' en el nombre) razona mal: **Probar IA** lo detecta con 3 casos de prueba.
- La IA solo puede elegir entre las categorías que la evidencia acústica hace posibles (no las 6), y no se le consulta
  cuando la red ya reconoce el sonido con claridad (≥ 60 %): menos errores y menos tokens. Las consultas van de una
  en una (LM Studio atiende una a la vez) con espera máxima de 90 s.
- La IA recibe primero un resumen en frases cortas ("La red oyó: Insect 95 %...") y el prompt trae ejemplos resueltos;
  así los modelos de 7-8B aciertan más. Si aun así la IA contradice lo que la red oyó con claridad, su respuesta se ignora.
- Inicia el servidor en la pestaña *Developer* de LM Studio. Si no hay modelo cargado, el programa lo carga solo con contexto 4096.
- **Probar IA** hace una consulta real de auditoría y dice si el modelo devolvió el JSON correcto y con qué formato
  (`json_schema` estricto, `json_object` o texto libre: el programa prueba en ese orden y recuerda el que funciona).
- Si el modelo "piensa en voz alta" (`<think>`), escribe texto antes del JSON o se corta, el programa lo limpia o reintenta
  una vez. Si LM Studio falla, la IA se pausa 30 s y el sistema sigue con la etapa acústica: las alertas no se detienen.
- Variables opcionales: `LMSTUDIO_URL`, `LMSTUDIO_MODELO`, `LMSTUDIO_TIMEOUT_S` (25), `LMSTUDIO_CONTEXTO` (4096),
  `IA_MODO` (`auto` o `siempre`), `RED_NEURONAL` (`0` para desactivar la red y usar solo reglas).

## SMS gratis: celular Android o Telegram
Twilio pide plan pago para enviar a Colombia. Dos alternativas gratuitas (pestaña *Alertas por SMS*):

**Proveedor «android»** (SMS reales desde tu celular, con tu plan de mensajes):
1. En el celular Android instala **SMS Gateway for Android** (Play Store o https://sms-gate.app) y dale permiso de SMS.
2. Activa **Local Server** y pulsa **Online**. La app muestra la dirección (p. ej. `http://192.168.1.20:8080`), un usuario y una contraseña.
3. En el programa: proveedor `android`, copia URL, usuario y contraseña, agrega los celulares en *Destinatarios*,
   **Guardar** y **Enviar SMS de prueba**. El PC y el celular deben estar en la misma red Wi-Fi.
   (Sin la misma red: en la app activa **Cloud Server** y usa la URL `https://api.sms-gate.app/3rdparty/v1/message`
   con el usuario y contraseña de la nube.)

**Proveedor «telegram»** (por internet, sin costo):
1. En Telegram habla con **@BotFather**, envía `/newbot` y copia el **token**.
2. Abre tu bot, pulsa **Iniciar** y escríbele "hola" (o crea un grupo con los guardabosques, agrega el bot y escribe algo).
3. En el programa: proveedor `telegram`, pega el token, pulsa **🔎 Detectar chats** (llena los ids solo) y **Enviar SMS de prueba**.

## SMS con Twilio: errores frecuentes
- El **número emisor** debe ir en formato internacional con `+` (por ejemplo `+15551234567`). Si lo escribes sin `+`, el
  programa ahora lo corrige al guardar.
- Si un envío sale con **error**, la columna de detalle del historial dice la causa en español (código de Twilio incluido).
  Lo más común en cuentas de prueba: **21608** (el celular destino no está verificado en Twilio) y **21408** (falta habilitar
  el país en *Messaging > Settings > Geo permissions*).

## Medir la precisión con audios reales
`python herramientas/evaluar_con_esc50.py` descarga ~600 audios reales del dataset público ESC-50 (solo la primera vez) y
muestra la detección de motosierras e incendios y las falsas alarmas con sonidos naturales y de máquinas. No toca tu base
de datos. ESC-50 no trae disparos reales: para disparos usa tus propias grabaciones con **Cargar Carpeta**.

## Entrenar el sistema con sus propias grabaciones (la mejora más grande en precisión)
La red YAMNet se entrenó con sonidos de internet; sus micrófonos y su bosque suenan distinto. Con grabaciones propias
etiquetadas el sistema aprende ese sonido real:
1. Crea una carpeta con una subcarpeta por categoría: `motosierra/`, `disparo/`, `incendio/`, `motor_vehiculo/`,
   `ambiente_natural/` (lluvia, viento, aves, insectos... ¡muchos!) y `otro/`. Ideal: 10 o más audios por categoría.
2. **🔒 Administración → 🎓 Entrenar con mis audios** y elige la carpeta (o `python herramientas/entrenar_con_mis_audios.py carpeta`).
3. Muestra la precisión con audios que el modelo no vio (validación cruzada) y la matriz de confusión. Se guarda en
   `modelos/ajuste_local.npz` y se usa de inmediato. Para quitarlo, borra ese archivo.

Prueba con el dataset ESC-50 (entrenado con unos audios y medido con otros que nunca vio):

| | Sin modelo propio | Con modelo propio |
|---|---|---|
| Motosierras detectadas | 81.2 % | **100 %** |
| Incendios detectados | 56.2 % | 62.5 % |
| Falsas alarmas (sonidos naturales) | 2.0 % | 2.0 % |
| Falsas alarmas (ruido humano/máquinas) | 12.5 % | 8.8 % |

Protecciones: el modelo propio solo decide cuando está muy seguro (≥ 80 %), no anula una amenaza que la red oye con
claridad y no puede crear una alerta si la red oye claramente un sonido natural que no estaba en el entrenamiento.

## Administración: pausar el análisis, apagar la IA y horario
Botón **🔒 Administración** (o clic en el indicador verde/naranja de la cabecera). Pide el PIN si hay uno.
- **⏸ Pausar / ▶ Activar análisis**: en pausa, el audio que envían los sensores se recibe pero **no se guarda ni se
  analiza** (no gasta IA ni disco) y no se generan alertas. La vigilancia de carpeta también espera.
- **Consultar la IA**: si se apaga, no se gasta ningún token de LM Studio; la red neuronal de audio y las alertas siguen.
  **En TODAS las señales** gasta más tokens (por defecto solo se consulta en casos dudosos). Se muestra cuántas
  consultas a la IA se hicieron hoy.
- **Horario de monitoreo**: analizar solo, por ejemplo, de 18:00 a 06:00 (cruza la medianoche). Fuera del horario se
  comporta como pausado y la cabecera muestra 🌙.
- **🔑 Cambiar PIN**: crea un PIN de 4 o más dígitos para que solo los administradores cambien esto (se guarda cifrado
  con hash en `configuracion.json`). Sin PIN, cualquiera puede cambiarlo.

También por consola (útil en un servidor sin ventana; el servidor de sensores aplica el cambio al instante):

    python monitoreo.py admin estado
    python monitoreo.py admin pausar --pin 1234
    python monitoreo.py admin activar --pin 1234
    python monitoreo.py admin ia-off --pin 1234          (ia-on para encenderla)
    python monitoreo.py admin horario 18:00 06:00 --pin 1234   (sin-horario para 24 h)
    python monitoreo.py admin pin 5678 --pin 1234        (crear o cambiar el PIN)

## 7. Datos
`base_de_datos/` (se crea sola) guarda audios, análisis, alertas y SMS. **Limpiar Todo** borra audios y resultados pero conserva sensores, tokens y destinatarios.

## 8. Cambios de esta versión
**Procesamiento de audio**
- Nueva red neuronal YAMNet que escucha el audio (antes solo había reglas con umbrales), con ganancia automática para fuentes lejanas.
- Nueva categoría **incendio** (el documento del proyecto la pedía y no existía).
- Conteo de **golpes** separados: un disparo son 1-3 golpes; fuego, gotas, pisadas o golpes de madera son muchos. Era la
  principal causa de falsos disparos.
- La medida de **actividad** se calcula contra el nivel alto del tramo (antes daba ~0 en motosierras reales).
- **Periodicidad** promediada en varias ventanas y filtrada (70-3500 Hz): estable con lluvia y viento.
- Los silencios digitales (cortes del sensor, relleno con ceros) ya no deforman las medidas.
- El filtro de silencio usa el tramo más fuerte: un disparo en una grabación larga y silenciosa ya no se pierde.

**Comunicación con la IA (LM Studio)**
- La IA recibe lo que **oyó la red** (etiquetas reales), las reglas y el **contexto** (hora, historial del sensor, vecinos),
  en ~150 tokens con nombres claros, en vez de 17 métricas abreviadas que solo repetían las reglas.
- Corregido: el esquema JSON no pedía la acción recomendada y el programa leía campos que no existían (siempre vacíos).
- Conexión HTTP reutilizada (más rápida), reintento único si la respuesta se corta o no es JSON, limpieza de `<think>`,
  confianzas tipo "85" convertidas a 0.85, formato aprendido reiniciado al cambiar de modelo.
- **Probar IA** hace una consulta real y verifica el JSON.

**SMS**
- Número emisor de Twilio normalizado (`+`), soporte de Messaging Service SID (`MG...`) y errores de Twilio explicados.
- El enfriamiento de 60 s es por sensor **y tipo de amenaza** (un disparo después de una motosierra sí se avisa).

**Resultados medidos con audios reales no usados para ajustar (ESC-50, folds 4 y 5)**

| | Versión anterior | Esta versión |
|---|---|---|
| Motosierras detectadas | 43.8 % | 81.2 % |
| Incendios detectados | 0 % (no existía) | 56.2 % |
| Falsas alarmas con sonidos naturales (304 audios) | 13.8 % | 2.0 % |
| Falsas alarmas con ruido humano/máquinas (160 audios) | 23.1 % | 12.5 % (más de la mitad es pólvora tomada como disparo) |
| Tiempo de la etapa acústica (5 s de audio) | 0.02 s | 0.10 s (meta ≤ 2 s) |

Sin cambios: interfaz, base de datos, servidor de sensores, comandos de consola, ruta óptima y cálculo de KPIs.

## Créditos
YAMNet © Google, licencia Apache 2.0 (https://github.com/tensorflow/models/tree/master/research/audioset/yamnet).
`herramientas/exportar_yamnet.py` convierte el modelo TFLite oficial al archivo `modelos/yamnet.npz` que usa el programa.
