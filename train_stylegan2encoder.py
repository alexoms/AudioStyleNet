import argparse
import numpy as np
import os
import random
import torch

from datetime import datetime
from glob import glob
from lpips import PerceptualLoss
from my_models import style_gan_2
from my_models.models import resnetEncoder
from PIL import Image
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from utils import datasets, utils
from torchvision import transforms
from torchvision.utils import save_image


HOME = os.path.expanduser('~')
RAIDROOT = os.environ.get('RAIDROOT')
DATAROOT = os.environ.get('DATAROOT')


class solverEncoder:
    def __init__(self, args):
        super().__init__()

        self.device = args.device
        self.args = args

        self.initial_lr = self.args.lr
        self.lr = self.args.lr
        self.lr_rampdown_length = 0.3
        self.lr_rampup_length = 0.1

        # Load generator
        self.g = style_gan_2.PretrainedGenerator1024().eval().to(self.device)
        for param in self.g.parameters():
            param.requires_grad = False
        self.latent_avg = self.g.latent_avg.repeat(
            18, 1).unsqueeze(0).to(self.device)

        # Init global step
        self.global_step = 0

        # Define encoder model
        self.e = resnetEncoder().train().to(self.device)

        # Print # parameters
        print("# params {} (trainable {})".format(
            utils.count_params(self.e),
            utils.count_trainable_params(self.e)
        ))

        # Select optimizer and loss criterion
        self.optim = torch.optim.Adam(self.e.parameters(), lr=self.initial_lr)
        self.criterion = PerceptualLoss(
            model='net-lin', net='vgg', gpu_id=args.gpu)

        # Load model and optimizer checkpoint
        if self.args.cont or self.args.test or self.args.run:
            path = self.args.model_path
            self.load(path)

        # Set up tensorboard
        if not self.args.debug and not self.args.test and not self.args.run:
            tb_dir = 'tensorboard_runs/encode_stylegan/' + \
                self.args.save_dir.split('/')[-2]
            self.writer = SummaryWriter(tb_dir)
            print(f"Logging run to {tb_dir}")

            # Create save dir
            os.makedirs(self.args.save_dir + 'models', exist_ok=True)

    def forward(self, img, evaluation=False):
        # Encode
        if evaluation:
            self.e.eval()
        latent_offset = self.e(img)
        if evaluation:
            self.e.train()
        # Add mean (we only want to compute offset to mean latent)
        latent = latent_offset + self.latent_avg

        # Decode
        img_gen, _ = self.g(
            [latent], input_is_latent=True, noise=self.g.noises)
        # Downsample to 256 x 256
        img_gen = utils.downsample_256(img_gen)

        # from torchvision.utils import make_grid
        # img_gen = make_grid(img_gen.detach().cpu(), normalize=True, range=(-1, 1))
        # transforms.ToPILImage()(img_gen).show()
        # 1 / 0

        # Compute perceptual loss
        loss = self.criterion(img_gen, img).mean()

        return loss, img_gen

    def save(self):
        save_path = f"{self.args.save_dir}models/model{self.global_step}.pt"
        torch.save({
            'model': self.e.state_dict(),
            'optim_state_dict': self.optim.state_dict(),
            'global_step': self.global_step,
        }, save_path)
        print(f"Saving: {save_path}")

    def load(self, path):
        print(f"Loading audio_encoder weights from {path}")
        checkpoint = torch.load(path, map_location=self.device)
        if type(checkpoint) == dict:
            self.optim.load_state_dict(checkpoint['optim_state_dict'])
            self.e.load_state_dict(checkpoint['model'])
            self.global_step = checkpoint['global_step']
        else:
            self.e.load_state_dict(checkpoint)

    def update_lr(self, t):
        lr_ramp = min(1.0, (1.0 - t) / self.lr_rampdown_length)
        lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
        lr_ramp = lr_ramp * min(1.0, t / self.lr_rampup_length)
        self.lr = self.initial_lr * lr_ramp
        self.optim.param_groups[0]['lr'] = self.lr

    def train(self, n_iters, train_loader, val_loader):
        print("Start training")
        val_loss = 0.0
        val_img = None
        val_img_gen = None

        pbar = tqdm()
        pbar.total = n_iters
        i_iter = 0
        while i_iter < n_iters:
            for batch in train_loader:
                # Unpack batch
                img = batch['img'].to(self.device)

                # Update learning rate
                t = self.global_step / n_iters
                self.update_lr(t)

                loss, img_gen = self.forward(img)

                # Optimize
                self.optim.zero_grad()
                loss.backward()
                self.optim.step()

                self.global_step += 1
                i_iter += 1
                pbar.update()

                # Update progress bar
                pbar.set_description('Step {gs} - '
                                     'Train loss {tl:.4f} - '
                                     'Val loss {vl:.4f} - '
                                     'lr {lr:.4f}'.format(
                                         gs=self.global_step,
                                         tl=loss,
                                         vl=val_loss,
                                         lr=self.lr
                                     ))

                if not self.args.debug:
                    if self.global_step % self.args.log_train_every == 0:
                        self.writer.add_scalars(
                            'loss', {'train': loss}, self.global_step)

                    if self.global_step % self.args.log_val_every == 0:
                        val_loss, val_img, val_img_gen = self.eval(val_loader)
                        self.writer.add_scalars(
                            'loss', {'val': val_loss}, self.global_step)

                    if self.global_step % self.args.save_every == 0:
                        self.save()

                    if self.global_step % self.args.save_img_every == 0:
                        # Save train sample
                        save_tensor = torch.cat(
                            (img.detach(), img_gen.detach().clamp(-1., 1.)), dim=0)
                        save_image(
                            save_tensor,
                            f'{self.args.save_dir}train_gen_{self.global_step}.png',
                            normalize=True,
                            range=(-1, 1),
                            nrow=min(8, self.args.batch_size)
                        )

                        if val_img is not None and val_img_gen is not None:
                            # Save validation sample
                            save_tensor = torch.cat(
                                (val_img.detach(), val_img_gen.detach().clamp(-1., 1.)), dim=0)
                            save_image(
                                save_tensor,
                                f'{self.args.save_dir}val_gen_{self.global_step}.png',
                                normalize=True,
                                range=(-1, 1),
                                nrow=min(8, self.args.batch_size)
                            )

                # Break if n_iters is reached and still in epoch
                if i_iter == n_iters:
                    break

        self.save()
        print('Done.')

    def eval(self, val_loader):
        # Train_sample
        batch = next(iter(val_loader))
        img = batch['img'].to(self.device)

        with torch.no_grad():
            # Forward
            loss, img_gen = self.forward(img, evaluation=True)

        return loss, img, img_gen

    def test_model(self, val_loader):
        # Generate image
        with torch.no_grad():
            # Generate random image
            z = torch.randn(self.args.batch_size, 512, device=self.device)
            img, _ = self.g([z], truncation=0.9,
                            truncation_latent=self.latent_avg)
            img = utils.downsample_256(img)

            # Forward
            _, img_gen = self.forward(img, evaluation=True)

        img_tensor = torch.cat((img, img_gen.clamp(-1., 1.)), dim=0)

        save_image(
            img_tensor,
            f'{self.args.save_dir}test_model_train.png',
            normalize=True,
            range=(-1, 1),
            nrow=min(8, self.args.batch_size)
        )

        # Test on validation data
        _, img_val, img_gen_val = self.eval(val_loader)
        save_tensor = torch.cat((img_val, img_gen_val.clamp(-1., 1.)), dim=0)
        save_image(
            save_tensor,
            f'{self.args.save_dir}test_model_val.png',
            normalize=True,
            range=(-1, 1),
            nrow=min(8, self.args.batch_size)
        )

    def run(self, src_path):
        if os.path.isdir(src_path):
            image_files = sorted(glob(src_path + '*.png'))
        else:
            image_files = [src_path]

        t = transforms.Compose([
            transforms.ToTensor(),
            utils.Downsample(256),
            transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
        ])

        # Set encoder to eval
        self.e.eval()

        # Project
        for file in tqdm(image_files):
            # Load and transform image
            img = t(Image.open(file)).unsqueeze(0).to(self.device)

            with torch.no_grad():
                # Encoder
                latent_offset = self.e(img)
                # Add mean (we only want to compute offset to mean latent)
                latent = latent_offset + self.latent_avg

                # Decode
                img_gen, _ = self.g(
                    [latent], input_is_latent=True, noise=self.g.noises)

            # Save results
            save_str = self.args.save_dir + 'encoded/' + \
                file.split('/')[-1].split('.')[0]
            os.makedirs(self.args.save_dir + 'encoded/', exist_ok=True)
            # print('Saving {}'.format(save_str + '_p.png'))
            save_image(img_gen, save_str + '_p.png',
                       normalize=True, range=(-1, 1))
            torch.save(latent.detach().cpu(), save_str + '.pt')

        # Set encoder back to train mode
        self.e.train()


if __name__ == '__main__':

    # Random seeds
    seed = 0
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', type=int)

    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--cont', action='store_true')
    parser.add_argument('--run', action='store_true')

    parser.add_argument('--batch_size', type=int, default=4)  # 4
    parser.add_argument('--lr', type=int, default=0.01)  # 0.01
    parser.add_argument('--n_iters', type=int, default=50000)  # 150000
    parser.add_argument('--log_train_every', type=int, default=100)  # 1
    parser.add_argument('--log_val_every', type=int, default=1000)   # 1000
    parser.add_argument('--save_img_every', type=int, default=10000)  # 10000
    parser.add_argument('--save_every', type=int, default=10000)  # 10000
    parser.add_argument('--save_dir', type=str,
                        default='saves/encode_stylegan/')
    parser.add_argument('--model_path', type=str, default=None)
    parser.add_argument('--src_path', type=str, default=None)
    args = parser.parse_args()

    if args.cont or args.test:
        assert args.model_path is not None

    # Correct path
    if args.save_dir[-1] != '/':
        args.save_dir += '/'
    args.save_dir += datetime.now().strftime("%Y-%m-%d_%H-%M-%S/")

    if args.cont or args.test or args.run:
        args.save_dir = '/'.join(args.model_path.split('/')[:-2]) + '/'

    if not args.debug:
        print("Saving run to {}".format(args.save_dir))
    else:
        print("DEBUG MODE")

    # Select device
    device = f'cuda:{args.gpu}'
    args.device = device
    torch.cuda.set_device(args.device)

    # Data loading
    ds = datasets.ImageDataset(
        root_path=DATAROOT + "AudioVisualDataset/Aligned256/",
        normalize=True,
        mean=[0.5, 0.5, 0.5],
        std=[0.5, 0.5, 0.5],
        image_size=256
    )
    train_loader = torch.utils.data.DataLoader(
        ds, batch_size=args.batch_size, shuffle=True, num_workers=4, pin_memory=True)
    val_loader = torch.utils.data.DataLoader(
        ds, batch_size=args.batch_size, shuffle=True, num_workers=4, pin_memory=True)
    print(len(ds))

    # Init solver
    solver = solverEncoder(args)

    # Train
    if args.test:
        solver.test_model(val_loader)
    elif args.run:
        solver.run(args.src_path)
    else:
        solver.train(args.n_iters, train_loader, val_loader)
