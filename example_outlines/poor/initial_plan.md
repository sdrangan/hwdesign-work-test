# Project Plan

## Team

Jordan

## IP

We will build a hardware accelerator for neural networks. Neural networks are
used everywhere in AI today and they are very slow on CPUs, so putting them in
hardware will make them much faster. Our IP will do the neural network
computations on the FPGA.

## Architecture

The IP will be one module that does all of the neural network processing. It
will connect to the processor using AXI. The processor sends the input and gets
the output back. We will figure out the details when we start implementing it.
