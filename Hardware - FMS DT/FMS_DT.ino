#include <SPI.h>
#include <Ethernet.h>

#include <ArduinoJson.h>
#include <Arduino_FreeRTOS.h>
#include <task.h>
#include <queue.h>
#include <semphr.h>

///////////////////////////////////////////////
//Variáveis
byte mac[] = { 0x00, 0xAD, 0xCE, 0xEB, 0xFE, 0xED };
IPAddress ip(192, 168, 10, 1);

EthernetServer serverUnity(8888);  //Servidor para conecção com unity
EthernetServer serverTripa(80);    //Servidor para receber posts (port 80 is default for HTTP):

EthernetClient clientUnity;  //Client que se conectou aka o unity do outo lado
int porto_I[3][6] = { -1 };
int porto_R[3][9] = { 0 };

QueueHandle_t envio;
SemaphoreHandle_t mutex_envio;

int saida;

bool digitalShadowON = false;

struct data_a_enviar {
  char pos_nome[5];  //Última posição tem de ser \0
  int high;
};

///////////////////////////////////////////////
//Inicializar portos
void inicializar_portos() {

  ///////////////////////////////////////////////
  //Inicializar os portos
  //Input
  pinMode(I0_0, INPUT);
  pinMode(I0_1, INPUT);
  pinMode(I0_2, INPUT);
  pinMode(I0_3, INPUT);
  pinMode(I0_4, INPUT);
  pinMode(I0_5, INPUT);

  pinMode(I1_0, INPUT);
  pinMode(I1_1, INPUT);
  pinMode(I1_2, INPUT);
  pinMode(I1_3, INPUT);
  pinMode(I1_4, INPUT);
  pinMode(I1_5, INPUT);

  pinMode(I2_0, INPUT);
  pinMode(I2_1, INPUT);
  pinMode(I2_2, INPUT);
  pinMode(I2_3, INPUT);
  pinMode(I2_4, INPUT);
  pinMode(I2_5, INPUT);

  //Output
  pinMode(R0_2, OUTPUT);
  pinMode(R0_3, OUTPUT);
  pinMode(R0_4, OUTPUT);
  pinMode(R0_5, OUTPUT);
  pinMode(R0_6, OUTPUT);
  pinMode(R0_7, OUTPUT);
  pinMode(R0_8, OUTPUT);

  pinMode(R1_2, OUTPUT);
  pinMode(R1_3, OUTPUT);
  pinMode(R1_4, OUTPUT);
  pinMode(R1_5, OUTPUT);
  pinMode(R1_6, OUTPUT);
  pinMode(R1_7, OUTPUT);
  pinMode(R1_8, OUTPUT);

  pinMode(R2_3, OUTPUT);
  pinMode(R2_4, OUTPUT);
  pinMode(R2_6, OUTPUT);
  pinMode(R2_7, OUTPUT);
  pinMode(R2_8, OUTPUT);
}

///////////////////////////////////////////////
//Função que devolve o valor dos sensores
int sensor(int portNumero, int portIndex) {
  switch (portNumero) {
    case 0:
      switch (portIndex) {
        case 0:
          return digitalRead(I0_0);
          break;
        case 1:
          return digitalRead(I0_1);
          break;
        case 2:
          return digitalRead(I0_2);
          break;
        case 3:
          return digitalRead(I0_3);
          break;
        case 4:
          return digitalRead(I0_4);
          break;
        case 5:
          return digitalRead(I0_5);
          break;
      }
      break;
    case 1:
      switch (portIndex) {
        case 0:
          return digitalRead(I1_0);
          break;
        case 1:
          return digitalRead(I1_1);
          break;
        case 2:
          return digitalRead(I1_2);
          break;
        case 3:
          return digitalRead(I1_3);
          break;
        case 4:
          return digitalRead(I1_4);
          break;
        case 5:
          return digitalRead(I1_5);
          break;
      }
      break;
    case 2:
      switch (portIndex) {
        case 0:
          return digitalRead(I2_0);
          break;
        case 1:
          return digitalRead(I2_1);
          break;
        case 2:
          return digitalRead(I2_2);
          break;
        case 3:
          return digitalRead(I2_3);
          break;
        case 4:
          return digitalRead(I2_4);
          break;
        case 5:
          return digitalRead(I2_5);
          break;
      }
      break;
  }
}

///////////////////////////////////////////////
//Função que atualiza os atuadores
int atuador(int portNumero, int portIndex, int high) {

  switch (portNumero) {
    case 0:
      switch (portIndex) {
        case 2:  //Punch Up
          digitalWrite(R0_2, high);
          break;
        case 3:  //Punch Back
          digitalWrite(R0_3, high);
          break;
        case 4:  //Conveyor E
          digitalWrite(R0_4, high);
          break;
        case 5:  //Conveyor E Rotate AntiClockwise
          digitalWrite(R0_5, high);
          break;
        case 6:  //Conveyor C Rotate Clockwise
          digitalWrite(R0_6, high);
          break;
        case 7:  // Conveyor A Backwards
          digitalWrite(R0_7, high);
          break;
        case 8:  // Conveyor A Left
          digitalWrite(R0_8, high);
          break;
      }
      break;
    case 1:
      switch (portIndex) {
        case 2:  //Punch Down
          digitalWrite(R1_2, high);
          break;
        case 3:  //Conveyor F
          digitalWrite(R1_3, high);
          break;
        case 4:  //Conveyor E Rotate Clockwise
          digitalWrite(R1_4, high);
          break;
        case 5:  //Conveyor E Rotate Clockwise
          digitalWrite(R1_5, high);
          break;
        case 6:  //Conveyor C
          digitalWrite(R1_6, high);
          break;
        case 7:  //Conveyor B
          digitalWrite(R1_7, high);
          break;
        case 8:  //Conveyor A Right
          digitalWrite(R1_8, high);
          break;
      }
      break;
    case 2:
      switch (portIndex) {
        case 3:  //Punch Spin
          digitalWrite(R2_3, high);
          break;
        case 4:  //Punch Forward
          digitalWrite(R2_4, high);
          break;
        case 6:  //Conveyor D
          digitalWrite(R2_6, high);
          break;
        case 7:  //Conveyor C Rotate AntiClockwise
          digitalWrite(R2_7, high);
          break;
        case 8:  //Conveyor A
          digitalWrite(R2_8, high);
          break;
      }
      break;
  }

  ///////////////////////////////////////////////
  //Atualizar os portos locais e digitais
  Atualiza_Portos_Locais('R', portNumero, portIndex, high);

  if (digitalShadowON) {
    data_a_enviar data;
    data.pos_nome[0] = 'R';
    data.pos_nome[1] = portNumero + '0';
    data.pos_nome[2] = '_';
    data.pos_nome[3] = portIndex + '0';
    data.pos_nome[4] = 0;
    data.high = high;

    xQueueSend(envio, &data, portMAX_DELAY);
  }
}

///////////////////////////////////////////////
//Função que atualiza os Portos digitais enviando apenas um porto mudado e um novo valor
void Atualiza_Portos_Digital(char *pos_nome, int high) {

  ///////////////////////////////////////////////
  // Envio do documento
  StaticJsonDocument<1024> Gson;

  Gson["Port"] = pos_nome;
  Gson["Value"] = high;  //enviamos em int para ocupar o mm espaço, isto é importante para o unity conseguir seguementar os pedidos na stream

  // Serialize the JSON document to a string
  String GsonString;
  serializeJson(Gson, GsonString);

  // Print the JSON string to the Serial Monitor
  taskENTER_CRITICAL(); 
  clientUnity.print(GsonString);
  taskEXIT_CRITICAL();
}

///////////////////////////////////////////////
//Função que atualiza os portos locais
void Atualiza_Portos_Locais(char porto, int portNumero, int portIndex, int high) {
  ///////////////////////////////////////////////
  //Colocar a nova informação nos bits
  switch (porto) {
    case 'I':
      porto_I[portNumero][portIndex] = high;
      break;
    case 'R':
      porto_R[portNumero][portIndex] = high;
      break;
  }
}

///////////////////////////////////////////////
//Funções de movimento
void transporte(bool modo) {  //1 ->x+ ; 0->x-
  if (modo && sensor(2, 2)) {
    atuador(1, 8, 1);
    while (sensor(2, 2)) {}
    atuador(1, 8, 0);
  } else if (!modo && sensor(2, 3)) {
    atuador(0, 8, 1);
    while (sensor(2, 3)) {}
    atuador(0, 8, 0);
  }
}

void passadeiraAB() {  // 1-> receber  0-> tirar
  if (sensor(0, 1) && !sensor(2, 2)) {
    atuador(2, 8, 1);
    atuador(1, 7, 1);
    while (!sensor(0, 0)) {}
    atuador(2, 8, 0);
    atuador(1, 7, 0);
  } else if (sensor(0, 1) && !sensor(2, 3)) {
    transporte(1);
    atuador(2, 8, 1);
    atuador(1, 7, 1);
    while (!sensor(0, 0)) {}
    atuador(2, 8, 0);
    atuador(1, 7, 0);
  }
}

void passadeiraBC() {  // 1-> receber  0-> tirar
  Serial.println("BC SENSORES: " + sensor(0, 0) + !sensor(1,4) + !sensor(1, 5) + sensor(1, 0));
  if (sensor(0, 0) && !sensor(1, 5) && !sensor(1, 0)) {
    atuador(1, 7, 1);  //B
    atuador(1, 6, 1);  //C
    while (!sensor(1, 0)) {}
    atuador(1, 7, 0);  //B
    atuador(1, 6, 0);  //C
  } else if (sensor(0, 0) && !sensor(1, 4) && !sensor(1, 0)) {
    Serial.println("ANTES Passadeira do c morreu BC");
    passadeiraC(1);
    Serial.println("DEPOIS Passadeira do c morreu BC");
    atuador(1, 7, 1);  //B
    atuador(1, 6, 1);  //C
    while (!sensor(1,0)) {}
    atuador(1, 7, 0);  //B
    atuador(1, 6, 0);  //C
  }
}

void passadeiraCD() {  // 1-> receber  0-> tirar
  if (sensor(1, 0) && !sensor(1, 4) && !sensor(1, 1)) {
    atuador(1, 6, 1);  //C
    atuador(2, 6, 1);  //D
    while (!sensor(1, 1)) {}
    atuador(1, 6, 0);  //C
    atuador(2, 6, 0);  //D
  } else if (sensor(1, 0) && !sensor(1, 5) && !sensor(1, 1)) {
    Serial.println("ANTES Passadeira do c morreu CD");
    passadeiraC(0);
    Serial.println("DEPOIS Passadeira do c morreu CD");
    atuador(1, 6, 1);  //C
    atuador(2, 6, 1);  //D
    while (!sensor(1, 1)) {}
    atuador(1, 6, 0);  //C
    atuador(2, 6, 0);  //D
  }
}

void passadeiraDE() {  // 1-> receber  0-> tirar
  if (sensor(1, 1) && !sensor(2, 0) && !sensor(1, 3)) {  // haver peça em D, nao haver nada em E, E estar virada para D
    atuador(2, 6, 1);                                    //D
    atuador(0, 4, 1);                                    //E
    while (!sensor(2, 0)) {}
    atuador(2, 6, 0);  //D
    atuador(0, 4, 0);  //E
  } else if (sensor(1, 1) && !sensor(2, 0) && !sensor(1, 2)) {
    Serial.println("ANTES Passadeira do c morreu DE");
    passadeiraE(1);
    Serial.println("DEPOIS Passadeira do c morreu DE");
    atuador(2, 6, 1);  //D
    atuador(0, 4, 1);  //E
    while (!sensor(2, 0)) {}
    atuador(2, 6, 0);  //D
    atuador(0, 4, 0);  //E
  } else {
  }
}

void passadeiraEF() {  // 1-> receber  0-> tirar
  if (sensor(2, 0) && !sensor(2, 1) && !sensor(1, 2)) {  // haver peça em E, nao haver nada em F, estar virada para F
    atuador(0, 4, 1);                                    //E
    atuador(1, 4, 1);                                    //F
    while (!sensor(2, 1)) {}
    atuador(0, 4, 0);                                           //E
    atuador(1, 4, 0);                                           //F
  } else if (sensor(2, 0) && !sensor(2, 1) && !sensor(1, 3)) {  // haver peça em E, nao haver nada em F, nao estar virada para F
    Serial.println("ANTES Passadeira do c morreu EF");
    passadeiraE(0);
    Serial.println("DEPOIS Passadeira do c morreu EF");
    atuador(0, 4, 1);  //E
    atuador(1, 4, 1);  //F
    while (!sensor(2, 1)) {}
    atuador(0, 4, 0);  //E
    atuador(1, 4, 0);  //F
  } else {
  }
}

void passadeiraFA() {  // 1-> receber  0-> tirar
  if (sensor(2, 1) && !sensor(0, 1) && !sensor(2, 3)) {  // haver peça em F, nao haver nada em A, estar em x-
    atuador(1, 4, 1);                                    //F
    atuador(0, 7, 1);                                    //A
    while (!sensor(0,1)) {}
    atuador(1, 4, 0);                                           //F
    atuador(0, 7, 0);                                           //A
  } else if (sensor(2, 1) && !sensor(0, 1) && !sensor(2, 2)) {  // haver peça em F, nao haver nada em A, estar em x+
    transporte(0);
    atuador(1, 4, 1);  //F
    atuador(0, 7, 1);  //A
    while (!sensor(0, 1)) {}
    atuador(1, 4, 0);  //F
    atuador(0, 7, 0);  //A
  }
}

void passadeiraC(int modo) {  //1->receber 0->tirar 2-calibrar
  if (modo == 1 && sensor(1, 5)) {
    atuador(2, 7, 1);
    while (sensor(1, 5)) {}
    atuador(2, 7, 0);
  } else if (modo == 0 && sensor(1, 4)) {
    atuador(0, 6, 1);
    while (sensor(1, 4)) {}
    atuador(0, 6, 0);
  } else if (modo == 2 && sensor(1, 4) && sensor(1, 5)) {
    atuador(2, 7, 1);
    while (sensor(1, 5)) {}
    atuador(2, 7, 0);
  }
}

void passadeiraE(bool modo) {  //1->receber 0->tirar
  if (modo && sensor(1, 3)) {
    atuador(0, 5, 1);
    while (sensor(1, 3)) {}
    atuador(0, 5, 0);
  } else if (!modo && sensor(1, 2)) {
    atuador(1, 5, 1);
    while (sensor(1, 2)) {}
    atuador(1, 5, 0);
  }
}

void estacao(int modo, int time) {  //1->screw 0->drll 2-> calibrate
  if (modo == 2) {
    atuador(0, 2, 1);        // motor estacao z+, cima
    while (sensor(0, 3)) {}  // ate sensor z+
    atuador(0, 2, 0);
    atuador(0, 3, 1);         //motor estacao y-, para trás
    while (!sensor(0, 5)) {}  // ate sensor y-
    atuador(0, 3, 0);
  } else if (modo == 1 && sensor(0, 5) && !sensor(0, 3) && sensor(1, 1)) {  // estar em y-, estar em z+ haver peça em D
    atuador(2, 4, 1);                                                       // avancar estacao
    while (!sensor(0, 4)) {}                                                // ate sensor y+
    atuador(2, 4, 0);                                                       //
    atuador(1, 3, 1);                                                       //descer estacao
    while (sensor(0, 2)) {}                                                 //ate sensor z-
    atuador(1, 3, 0);
    atuador(2, 3, 1);  //brush
    delay(time);
    atuador(2, 3, 0);
    atuador(0, 2, 1);        // motor estacao z+, cima
    while (sensor(0, 3)) {}  // ate sensor z+
    atuador(0, 2, 0);
    atuador(0, 3, 1);         //motor estacao y-, para trás
    while (!sensor(0, 5)) {}  // ate sensor y-
    atuador(0, 3, 0);
  } else if (modo == 0 && sensor(0, 5) && !sensor(0, 3) && sensor(1, 1)) {  // estar em y-, estar em z+ haver peça em D
    atuador(2, 4, 1);                                                       // avancar estacao
    while (!sensor(0, 4)) {}                                                // ate sensor y+
    atuador(2, 4, 0);                                                       //
    atuador(2, 3, 1);                                                       //brush
    atuador(1, 3, 1);                                                       //descer estacao
    while (sensor(0, 2)) {}                                                 //ate sensor z-
    atuador(1, 3, 0);
    delay(time);
    atuador(2, 3, 0);
    atuador(0, 2, 1);        // motor estacao z+, cima
    while (sensor(0, 3)) {}  // ate sensor z+
    atuador(0, 2, 0);
    atuador(0, 3, 1);         //motor estacao y-, para trás
    while (!sensor(0, 5)) {}  // ate sensor y-
    atuador(0, 3, 0);
  }
}

///////////////////////////////////////////////
//Tasks
void TaskTripa(void *pvParameters) {
  // Client variables
  while(1) {
    String readString;
    String response;
    String skill;
    Serial.println("Teste1");
    EthernetClient clientTripa = serverTripa.available();
    if (clientTripa) {
      Serial.println("Teste2");
      while (clientTripa.connected()) {
        if (clientTripa.available()) {
          char c = clientTripa.read();
          readString += c;

          //if HTTP request has ended
          if (c == '\n') {

            if (readString.indexOf("/passadeiras") > 0) {
              if (readString.indexOf("?skill=") > 0) {
                Serial.println("Teste3");
                int pos1 = readString.indexOf("=");
                int pos2 = readString.indexOf("HTTP");
                skill = readString.substring(pos1 + 1, pos2 - 1);
                
                if (skill == "AB") {
                  Serial.println("AB ENTREI");
                  if (digitalShadowON) {
                    data_a_enviar d_skill;
                    strcpy(d_skill.pos_nome, "AB");
                    d_skill.high = 1;
                    xQueueSend(envio, &d_skill, portMAX_DELAY);
                  }
                  passadeiraAB();
                  Serial.println("AB SAI");
                  response = "skill AB done";
                } else if (skill == "BC") {
                  Serial.println("BC ENTREI");
                  if (digitalShadowON) {
                    data_a_enviar d_skill;
                    strcpy(d_skill.pos_nome, "BC");
                    d_skill.high = 1;
                    xQueueSend(envio, &d_skill, portMAX_DELAY);
                  }
                  passadeiraBC();
                  Serial.println("BC SAI");
                  response = "skill BC done";
                } else if (skill == "CD") {
                  Serial.println("CD ENTREI");
                  if (digitalShadowON) {
                    data_a_enviar d_skill;
                    strcpy(d_skill.pos_nome, "CD");
                    d_skill.high = 1;
                    xQueueSend(envio, &d_skill, portMAX_DELAY);
                  }
                  passadeiraCD();
                  Serial.println("CD SAI");
                  response = "skill CD done";
                } else if (skill == "DE") {
                  Serial.println("DE ENTREI");
                  if (digitalShadowON) {
                    data_a_enviar d_skill;
                    strcpy(d_skill.pos_nome, "DE");
                    d_skill.high = 1;
                    xQueueSend(envio, &d_skill, portMAX_DELAY);
                  }
                  passadeiraDE();
                  Serial.println("DE SAI");
                  response = "skill DE done";
                } else if (skill == "EF") {
                  Serial.println("EF ENTREI");
                  if (digitalShadowON) {
                    data_a_enviar d_skill;
                    strcpy(d_skill.pos_nome, "EF");
                    d_skill.high = 1;
                    xQueueSend(envio, &d_skill, portMAX_DELAY);
                  }
                  passadeiraEF();
                  Serial.println("EF SAI");
                  response = "skill EF done";
                } else if (skill == "FA") {
                  Serial.println("FA ENTREI");
                  if (digitalShadowON) {
                    data_a_enviar d_skill;
                    strcpy(d_skill.pos_nome, "FA");
                    d_skill.high = 1;
                    xQueueSend(envio, &d_skill, portMAX_DELAY);
                  }
                  passadeiraFA();
                  Serial.println("FA SAI");
                  response = "skill FA done";
                }
              }
            }
            Serial.println("Teste4");
            if (readString.indexOf("/estacao") > 0) {
              if (readString.indexOf("?skill=") > 0) {
                int pos1 = readString.indexOf("=");
                int pos2 = readString.indexOf("HTTP");
                skill = readString.substring(pos1 + 1, pos2 - 1);
                if (digitalShadowON) {
                  data_a_enviar d_skill;
                  memset(d_skill.pos_nome, 0, sizeof(d_skill.pos_nome));
                  skill.toCharArray(d_skill.pos_nome, 5);
                  d_skill.high = 1;
                  xQueueSend(envio, &d_skill, portMAX_DELAY);
                }
                estacao(skill.toInt(), 3000);
                response = "skill done";
              }
            }
            if (readString.indexOf("/sensores") > 0) {
              String sensor1 = String(digitalRead(I2_4));
              String sensor2 = String(digitalRead(I2_5));

              response = sensor1 + ";" + sensor2 + ";";
            }
            Serial.println("Teste5");

            taskENTER_CRITICAL();
            clientTripa.println("HTTP/1.1 200 OK\nContent-Type: text/html\nConnection: close\nRefresh: 5\n\n" + response);
            Serial.println("Teste6");
            clientTripa.stop();
            taskEXIT_CRITICAL();
            Serial.println("Boom");
            readString = "";
          }
        }
      }
    }
    vTaskDelay(1);
  }
}

void TaskEnvio(void *pvParameters) {
  data_a_enviar data;
  while(1) {
    ///////////////////////////////////////////////
    //Envio do pedido para o unity
    xQueueReceive(envio, &data, portMAX_DELAY);  //Queue utilizada para conseguirmos acumular pedidos de atualização
    if (digitalShadowON) {  //Precisamos deste if para assegurar que após o unity se ter desligado não são enviados mais nenhuns updates de sensor
      Atualiza_Portos_Digital(data.pos_nome, data.high);
    }
    vTaskDelay(1);
  }
}

void TaskSensores(void *pvParameters) {

  data_a_enviar data;
  data.pos_nome[0] = 'I';
  data.pos_nome[2] = '_';
  data.pos_nome[4] = 0;
  
  while(1) {
    if (digitalShadowON) {

      ///////////////////////////////////////////////
      //Sensores no porto I0
      data.pos_nome[1] = '0';

      ///////////////////////////////////////////////
      //I0_0
      saida = sensor(0, 0);
      if (porto_I[0][0] != saida) {

        Atualiza_Portos_Locais('I', 0, 0, saida);

        data.high = saida;
        data.pos_nome[3] = '0';

        xQueueSend(envio, &data, portMAX_DELAY);
      }
      ///////////////////////////////////////////////
      //I0_1
      saida = sensor(0, 1);
      if (porto_I[0][1] != saida) {

        Atualiza_Portos_Locais('I', 0, 1, saida);

        data.high = saida;
        data.pos_nome[3] = '1';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I0_2
      saida = sensor(0, 2);
      if (porto_I[0][2] != saida) {

        Atualiza_Portos_Locais('I', 0, 2, saida);

        data.high = saida;
        data.pos_nome[3] = '2';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I0_3
      saida = sensor(0, 3);
      if (porto_I[0][3] != saida) {

        Atualiza_Portos_Locais('I', 0, 3, saida);

        data.high = saida;
        data.pos_nome[3] = '3';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I0_4
      saida = sensor(0, 4);
      if (porto_I[0][4] != saida) {

        Atualiza_Portos_Locais('I', 0, 4, saida);

        data.high = saida;
        data.pos_nome[3] = '4';

        xQueueSend(envio, &data, portMAX_DELAY);
      }
      ///////////////////////////////////////////////
      //I0_5
      saida = sensor(0, 5);
      if (porto_I[0][5] != saida) {

        Atualiza_Portos_Locais('I', 0, 5, saida);

        data.high = saida;
        data.pos_nome[3] = '5';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //Sensores no porto I1
      data.pos_nome[1] = '1';

      ///////////////////////////////////////////////
      //I1_0
      saida = sensor(1, 0);
      if (porto_I[1][0] != saida) {

        Atualiza_Portos_Locais('I', 1, 0, saida);

        data.high = saida;
        data.pos_nome[3] = '0';

        xQueueSend(envio, &data, portMAX_DELAY);
      }
      ///////////////////////////////////////////////
      //I1_1
      saida = sensor(1, 1);
      if (porto_I[1][1] != saida) {

        Atualiza_Portos_Locais('I', 1, 1, saida);

        data.high = saida;
        data.pos_nome[3] = '1';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I1_2
      saida = sensor(1, 2);
      if (porto_I[1][2] != saida) {

        Atualiza_Portos_Locais('I', 1, 2, saida);

        data.high = saida;
        data.pos_nome[3] = '2';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I1_3
      saida = sensor(1, 3);
      if (porto_I[1][3] != saida) {

        Atualiza_Portos_Locais('I', 1, 3, saida);

        data.high = saida;
        data.pos_nome[3] = '3';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I1_4
      saida = sensor(1, 4);
      if (porto_I[1][4] != saida) {

        Atualiza_Portos_Locais('I', 1, 4, saida);

        data.high = saida;
        data.pos_nome[3] = '4';

        xQueueSend(envio, &data, portMAX_DELAY);
      }
      ///////////////////////////////////////////////
      //I1_5
      saida = sensor(1, 5);
      if (porto_I[1][5] != saida) {

        Atualiza_Portos_Locais('I', 1, 5, saida);

        data.high = saida;
        data.pos_nome[3] = '5';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //Sensores no porto I2
      data.pos_nome[1] = '2';

      ///////////////////////////////////////////////
      //I2_0
      saida = sensor(2, 0);
      if (porto_I[2][0] != saida) {

        Atualiza_Portos_Locais('I', 2, 0, saida);

        data.high = saida;
        data.pos_nome[3] = '0';

        xQueueSend(envio, &data, portMAX_DELAY);
      }
      ///////////////////////////////////////////////
      //I2_1
      saida = sensor(2, 1);
      if (porto_I[2][1] != saida) {

        Atualiza_Portos_Locais('I', 2, 1, saida);

        data.high = saida;
        data.pos_nome[3] = '1';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I2_2
      saida = sensor(2, 2);
      if (porto_I[2][2] != saida) {

        Atualiza_Portos_Locais('I', 2, 2, saida);

        data.high = saida;
        data.pos_nome[3] = '2';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I2_3
      saida = sensor(2, 3);
      if (porto_I[2][3] != saida) {

        Atualiza_Portos_Locais('I', 2, 3, saida);

        data.high = saida;
        data.pos_nome[3] = '3';

        xQueueSend(envio, &data, portMAX_DELAY);
      }

      ///////////////////////////////////////////////
      //I2_4
      saida = sensor(2, 4);
      if (porto_I[2][4] != saida) {

        Atualiza_Portos_Locais('I', 2, 4, saida);

        data.high = saida;
        data.pos_nome[3] = '4';

        xQueueSend(envio, &data, portMAX_DELAY);
      }
      ///////////////////////////////////////////////
      //I2_5
      saida = sensor(2, 5);
      if (porto_I[2][5] != saida) {

        Atualiza_Portos_Locais('I', 2, 5, saida);

        data.high = saida;
        data.pos_nome[3] = '5';

        xQueueSend(envio, &data, portMAX_DELAY);
      }
    }
    vTaskDelay(1);
  }
}

void TaskDigitalshadow(void *pvParameters) {
  
  while(1) {
    ///////////////////////////////////////////////
    //Conecção com Unity
    if (digitalShadowON) {
      if (!clientUnity.connected()) {
        Serial.println("\nDigital Disconnected");

        //Quando desconectar avisar o programa que o digitalshadow está desligado
        digitalShadowON = false;
      }
    } else {
      clientUnity = serverUnity.accept();
      if (clientUnity) {
        Serial.println("\nDigital Connected");
        //Resetar informação local
        for (int i = 0; i < 3; i++) {
          for (int j = 0; j < 6; j++) {
            porto_I[i][j] = -1;
          }
        }
        //Avisar o programa que o digitalshadow está ligado
        digitalShadowON = true; //MUDEI
      }
    }
    vTaskDelay(1);
  }
}

///////////////////////////////////////////////
//Setup
void setup() {

  ///////////////////////////////////////////////
  //Pin Declaration
  inicializar_portos();

  ///////////////////////////////////////////////
  // Open serial communications and wait for port to open:
  Serial.begin(9600);
  Serial.println("Kit Tripa mas bem feito");

  ///////////////////////////////////////////////
  // start the Ethernet
  Ethernet.begin(mac, ip);

  //////////////////////////////////////////////
  // Check for Ethernet hardware present
  if (Ethernet.hardwareStatus() == EthernetNoHardware) {
    Serial.println("Ethernet shield was not found.  Sorry, can't run without hardware. :(");
    while (true) {
      delay(1);  // do nothing, no point running without Ethernet hardware
    }
  }
  if (Ethernet.linkStatus() == LinkOFF) {
    Serial.println("Ethernet cable is not connected.");
  }

  ///////////////////////////////////////////////
  //Começar os 2 servidores
  Serial.println("Starting Servers");
  serverTripa.begin();
  serverUnity.begin();
  Serial.print("Servers at ");
  Serial.println(Ethernet.localIP());

  ///////////////////////////////////////////////
  //Inicializar Queue
  envio = xQueueCreate(30, sizeof(data_a_enviar));
  mutex_envio = xSemaphoreCreateMutex();

  ///////////////////////////////////////////////
  //Task Creation
  xTaskCreate(TaskTripa,  // Task function
              "Tripa",    // Task name for humans
              512,
              NULL,  // Task parameter
              NULL,     // Task priority
              NULL);

  xTaskCreate(TaskDigitalshadow,  // Task function
              "DigitalShadow",    // Task name for humans
              256,
              NULL,  // Task parameter
              NULL,     // Task priority
              NULL);
  
  xTaskCreate(TaskSensores,  // Task function
              "Sensores",    // Task name for humans
              512,
              NULL,  // Task parameter
              NULL,     // Task priority
              NULL);
  
  xTaskCreate(TaskEnvio,  // Task function
              "Envio",    // Task name for humans
              512,
              NULL,  // Task parameter
              NULL,     // Task priority
              NULL);
  
  Serial.println("It is working, will start...");
}

///////////////////////////////////////////////
//Loop
void loop() {}