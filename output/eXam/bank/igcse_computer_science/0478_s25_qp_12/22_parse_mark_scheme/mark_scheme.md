# Mark Scheme

### Q1a

**Mark scheme:**

> Any one from:
> \begin{itemize}
> \item ROM
> \item Cache
> \end{itemize}

---

### Q1b

**Mark scheme:**

> Any one from:
> \begin{itemize}
> \item It is volatile storage // Data is lost when the power is turned off.
> \item It needs to be \textbf{regularly} replaced by other data // The data \textbf{regularly} changes // Data needs to be \textbf{constantly} updated
> \end{itemize}

---

### Q1c

**Mark scheme:**

> \begin{itemize}
> \item 10011
> \item 11100110
> \end{itemize}

---

### Q1d

**Mark scheme:**

> \begin{itemize}
> \item 0011 0101
> \item 1000 1010 1101
> \end{itemize}

---

### Q1e

**Mark scheme:**

> One mark for each correct nibble (MAX 2)
> One mark for a correct method of working e.g. showing carries.
> \begin{alltt}
>    1 1
>    0 1 1 0 0 1 0 1
>  + 0 1 1 1 0 0 0 0
>    ---------------
>    1 1 0 1 0 1 0 1
> \end{alltt}

---

### Q1f

**Mark scheme:**

> Any two from:
> \begin{itemize}
> \item The \textbf{result} is greater than 255 // The \textbf{result} is too large.
> \item Cannot be stored in \textbf{8 bits} // Cannot be stored in the number of bits available for the \textbf{register}
> \end{itemize}

---

### Q1g

**Mark scheme:**

> One mark for a correct working method e.g. flip and add
> One mark for correct answer
> \newline
> 11101010

---

### Q2a

**Answer:** A

---

### Q2bi

**Mark scheme:** 3

---

### Q2bii

**Mark scheme:** 2

---

### Q2ci

**Mark scheme:** 32 bits are used to represent each/a/one colour in the image // $2^{32}$ (approx. 4.3 billion) different colours can be used/are available for the image

---

### Q2cii

**Mark scheme:**

> \textbf{Any two from:}
> \begin{itemize}
> \item The size of the image \textbf{file} increases.
> \item ... as the number of bits used to represent/store a colour has increased
> \end{itemize}

---

### Q2d

**Mark scheme:**

> \textbf{Any three from:}
> \begin{itemize}
> \item The size of the file is reduced without \textbf{permanently} removing any data.
> \item A compression algorithm is used.
> \item ... such as Run length encoding/RLE.
> \item Repeating \textbf{pixels} are grouped/identified ... // \textbf{Patterns} are identified ...
> \item ... and stored with the number of times they are repeated.
> \item ... and indexed
> \end{itemize}

---

### Q3a

**Mark scheme:**

> \textbf{Any two from:}
> \begin{itemize}
> \item It is easier to debug.
> \item Less likely to make errors.
> \item The program is machine independent/portable
> \end{itemize}

---

### Q3bi

**Mark scheme:**

> \textbf{One mark for each correct term in the correct place.}
> \begin{itemize}
> \item whole code
> \item executing
> \item all
> \item line by line
> \item error
> \end{itemize}
> A compiler translates the \textbf{whole code} at once before \textbf{executing} it. A compiler produces an error report that displays \textbf{all} errors.
> \newline
> An interpreter translates and executes the code \textbf{line by line}. An interpreter stops execution when an \textbf{error} is found and continues once it is corrected.

---

### Q3bii

**Mark scheme:**

> One mark for each correct function. One mark for each correct matching role description.
> 
> Examples:
> \begin{itemize}
> \item Code editor
> \item Allows the programmer to write/change the program.
> \item Run-time environment
> \item Allows the user to run the code and see the output.
> \item Error \textbf{diagnostics}
> \item Features that can be used to find errors in the code
> \item Auto-completion
> \item A programmer starts to type a command word, and the IDE suggests \textbf{option} for completing it.
> \item Auto-correction
> \item If a programmer \textbf{misspells} a command word it is changed to the correct spelling
> \item Prettyprint
> \item The \textbf{command words/identifiers} are given different colours
> \end{itemize}

---

### Q3ci

**Mark scheme:**

> Any \textbf{one} from:
> \begin{itemize}
> \item Inkjet
> \item Laser
> \end{itemize}

---

### Q3cii

**Mark scheme:**

> \textbf{One mark from:}
> \begin{itemize}
> \item Serial
> \item Parallel
> \end{itemize}
> \textbf{One mark from:}
> \begin{itemize}
> \item Half-duplex
> \item Full-duplex
> \item Simplex
> \end{itemize}
> \textbf{Any four from (for descriptions matching transmission types given):}
> \newline
> \textbf{serial}
> \begin{itemize}
> \item Serial would send bits in order // serial uses only one wire.
> \item ... so won't be skewed // less likely to have errors
> \item Serial transmission speed would be adequate.
> \end{itemize}
> \textbf{parallel}
> \begin{itemize}
> \item Parallel would transmit data faster.
> \item ... as multiple bits are sent at the \textbf{same time} // ... as multiple wires are used.
> \item For parallel, chance of \textbf{skewing/errors} would be low as \textbf{short distance} transmission only required.
> \end{itemize}
> \textbf{half/full duplex}
> \begin{itemize}
> \item To allow data to be sent in both directions
> \item ... so any interrupts/notifications for errors can be sent back to the computer.
> \end{itemize}
> \textbf{simplex}
> \begin{itemize}
> \item Data \textbf{only} needs to be sent one direction // Data transmission doesn't need to be two-way.
> \item ... as the printer may not need to send errors back to the computer
> \end{itemize}

---

### Q3ciii

**Mark scheme:**

> \begin{itemize}
> \item A parity bit is added to each \textbf{byte}.
> \item … to make the number of 1s/0s even // that will be 1 if the number of 1s/0s is odd // that will be 0 if the number of 1s/0s is even.
> \item The number of 1s/0s in each byte is counted \textbf{after transmission}.
> \item If any bytes have an odd number of 1s/0s an error is detected
> \end{itemize}

---

### Q4a

**Mark scheme:**

> One mark for each correct component or description
> \newline
> \begin{tabular}{|c|l|}
> \hline
> \textbf{Component} & \multicolumn{1}{c|}{\textbf{Description}} \\ \hline
> \textbf{Control unit // CU} & It sends signals to all the components in the CPU to manage the flow of data through the CPU. \\ \hline
> \textbf{Arithmetic and logic unit // ALU} & It carries out all the arithmetic and logic operations in the CPU. \\ \hline
> Cache & \textbf{It stores frequently used data/instructions} \\ \hline
> Program counter (PC) & \textbf{It stores the address of the next instruction to be fetched} \\ \hline
> \textbf{Clock} & It controls the number of fetch-decode-execute (FDE) cycles that are performed per second. \\ \hline
> \textbf{Memory data register // MDR} & It stores data immediately before it is transmitted to RAM and immediately after it is received from RAM. \\ \hline
> \end{tabular}

---

### Q4b

**Mark scheme:**

> \textbf{Two from:}
> \begin{itemize}
> \item An embedded system is designed to perform a dedicated/limited/single function // computer can be used to perform many different functions.
> \item An embedded system has \textbf{dedicated} hardware // computer has hardware that can be used by other devices.
> \item An embedded system has software that is not easily updated/reprogrammed // software can easily be updated/reprogrammed on the computer.
> \item An embedded system has a microprocessor // A computer has a CPU.
> \item An embedded system can be part of/built into a larger device // A computer is normally standalone
> \end{itemize}

---

### Q5a

**Mark scheme:**

> \textbf{Three from:}
> \begin{itemize}
> \item The \textbf{inference} engine is used …
> \item … to \textbf{decide} which questions to ask the user …
> \item … based on the \textbf{previous} data input.
> \item Symptoms input are located in/compared to knowledge base.
> \item … then applies the rule base to the knowledge base (to decide the diagnosis)
> \end{itemize}

---

### Q5bi

**Mark scheme:**

> Any \textbf{two} from:
> \newline
> Examples:
> \begin{itemize}
> \item Sensors
> \item Microprocessors
> \item Actuators
> \end{itemize}

---

### Q5bii

**Mark scheme:**

> Any \textbf{four} from:
> \newline
> Examples:
> \begin{itemize}
> \item a doctor doesn’t need to \textbf{travel} to the hospital to do the surgery.
> \item … so it can be done by any specialist/doctor in the world.
> \item … so it can be done immediately without needing to wait for travel time // reduces the waiting time for the patient.
> \item … so the doctor may be better at the surgery as they won’t be tired from travel.
> \item … so travel costs are saved.
> \item Surgery with robots enhances precision/accuracy.
> \item … so a smaller incision can be made.
> \item … so the recovery time may be shorter.
> \item … as the components used to enter the body can be much smaller than a human hand.
> \item … so the surgery is safer/ more hygienic // by example e.g. can stop the doctor needing to be near an infectious patient.
> \item The surgery may have a higher rate of success
> \end{itemize}

---

### Q5biii

**Mark scheme:**

> \textbf{Two} from (one for a point and one for the matching expansion):
> \newline
> Examples:
> \begin{itemize}
> \item The internet connection could be lost/delayed …
> \item … so the surgery may not be able to continue.
> \item The robot will be expensive to buy/maintain …
> \item … this money could have been spent on other causes.
> \item The robot could be hacked ….
> \item … and endanger the patient’s life.
> \item Data could be corrupted in transmission …
> \item … changing the nature of the instruction for the robot.
> \item The robot’s hardware could malfunction …
> \item … so the surgery cannot continue
> \end{itemize}

---

### Q5ci

**Mark scheme:**

> \textbf{One mark for each correct part of the diagram.}
> \newline
> The diagram:
> \begin{itemize}
> \item Web browser identified as software used to \textbf{send} URL/requests or \textbf{receives} IP address/web page data
> \item URL/domain sent to DNS
> \item DNS \textbf{searches} for \textbf{matching} IP address
> \item If not found sent to another DNS
> \item IP address sent from DNS to patient's computer
> \item Request sent from patient's computer to \textbf{web server} (for web page)
> \item Web page/HTML data sent from web server to patient's computer
> \end{itemize}

---

### Q5cii

**Mark scheme:**

> Any \textbf{six} from:
> \begin{itemize}
> \item Encrypted connection established …
> \item … using asymmetric encryption
> \item … to make any data sent meaningless
> \item The \textbf{web browser} asks the \textbf{web server} to identify itself.
> \item … by sending its digital certificate.
> \item The digital certificate is authenticated/validated by the \textbf{web browser}.
> \item If the certificate is authenticated/valid, the connection is secure/the transaction can begin.
> \item If the certificate is not authenticated/invalid, the connection is not secure/the transaction is cancelled/rejected/user is notified
> \end{itemize}
