###############################################################################

ln -s $PWD/../applegpu $PWD/extra/disassemblers/applegpu

###############################################################################

pip install -e .

python -c "from tinygrad import Device; print(f'Device.DEFAULT: {Device.DEFAULT}')"

###############################################################################
