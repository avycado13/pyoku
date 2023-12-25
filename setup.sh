#!/bin/bash

# Save the current directory
current_dir=$(pwd)

# Navigate to the user's home directory
cd ~

# Create a bare git repository in the home directory
mkdir pyoku && cd pyoku && git init --bare

# Go back to the original directory
cd $current_dir

# Install dependencies (replace 'pip install -r requirements.txt' with your actual command)
pip install -r requirements.txt

# Move main.py and post-receive to .git/hooks/ in the newly created bare repository
mv main.py ~/pyoku/.git/hooks/
mv post-receive ~/pyoku/.git/hooks/
